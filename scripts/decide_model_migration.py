"""Publish and independently verify one EL-3 model-migration decision."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.compiler import validate_compile_decision, validate_gap_report
from harness.evaluation import model_fingerprint_digest, model_path_fingerprint
from harness.model_migration import (
    base_arm_from_gap,
    build_migration_report,
    publish_migration_report,
)
from utils.s3_utils import S3Utils


def _read(services: ReadOnlyServices, ref: str, expected_sha256: str) -> bytes:
    body = services.object_body(ref)
    if body is None or sha256(body) != expected_sha256:
        raise ValueError(f"migration_object_hash_mismatch:{ref}")
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--gap-report-ref", required=True)
    parser.add_argument("--gap-report-sha256", required=True)
    parser.add_argument("--compile-decision-ref", required=True)
    parser.add_argument("--compile-decision-sha256", required=True)
    parser.add_argument("--target-fingerprint-sha256", required=True)
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--min-improvement", type=float, default=0.01)
    parser.add_argument("--max-p95-regression-ratio", type=float, default=1.2)
    parser.add_argument("--max-training-cost", type=float, default=1.0)
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("migration_database_url_missing")
    if not args.verifier_database_url or args.verifier_database_url == args.database_url:
        raise ValueError("migration_verifier_database_url_missing")

    identity = {"tenant_id": args.tenant_id, "username": "el3-verifier", "role": "admin"}
    services = ReadOnlyServices(args.verifier_database_url, identity)
    gap = validate_gap_report(
        json.loads(_read(services, args.gap_report_ref, args.gap_report_sha256))
    )
    decision = validate_compile_decision(
        json.loads(
            _read(
                services,
                args.compile_decision_ref,
                args.compile_decision_sha256,
            )
        )
    )
    target = next(
        (
            item
            for item in gap["targets"]
            if item["fingerprint_sha256"] == args.target_fingerprint_sha256
        ),
        None,
    )
    if target is None or decision["target"] != target:
        raise ValueError("migration_target_mismatch")
    actual = model_path_fingerprint(target["fingerprint"]["model_id"], model_root=args.model_root)
    if model_fingerprint_digest(actual) != args.target_fingerprint_sha256:
        raise ValueError("migration_target_fingerprint_mismatch")

    outcomes = [
        next(
            item
            for item in task["outcomes"]
            if item["target_fingerprint_sha256"] == args.target_fingerprint_sha256
        )
        for task in gap["tasks"]
    ]
    transcripts = {
        item["transcript_ref"]: json.loads(
            _read(services, item["transcript_ref"], item["transcript_sha256"])
        )
        for item in outcomes
    }
    base = base_arm_from_gap(
        gap,
        args.target_fingerprint_sha256,
        transcripts,
        gap_report_ref=args.gap_report_ref,
        gap_report_sha256=args.gap_report_sha256,
    )
    report = build_migration_report(
        tenant_id=args.tenant_id,
        target_fingerprint=target["fingerprint"],
        learning_source={
            "kind": "compile_decision",
            "ref": args.compile_decision_ref,
            "sha256": args.compile_decision_sha256,
            "reason": decision["reason"],
            "value": decision,
        },
        arms=[base],
        policy={
            "version": "model-migration@1",
            "min_pass_rate": args.min_pass_rate,
            "min_improvement": args.min_improvement,
            "max_p95_regression_ratio": args.max_p95_regression_ratio,
            "max_training_cost": args.max_training_cost,
        },
    )
    s3 = S3Utils()
    published = publish_migration_report(S3EvidenceStore(s3.bucket, s3.client), report)
    verified = (
        default_verifiers()
        .get("verify_model_migration", 1)
        .handler({"parameters": published}, identity, {}, services)
    )
    if verified.status != "passed":
        raise RuntimeError(f"migration_verification_failed:{verified.error_code}")
    print(json.dumps({**published, "decision": report["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()
