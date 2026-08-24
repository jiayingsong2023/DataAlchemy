"""Publish an independently verified base versus gap-SFT migration decision."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.compiler import validate_compile_manifest, validate_gap_report
from harness.model_migration import (
    base_arm_from_gap,
    build_migration_report,
    candidate_arm_from_gap,
    publish_migration_report,
)
from utils.s3_utils import S3Utils


def _read(services: ReadOnlyServices, ref: str, expected: str) -> bytes:
    body = services.object_body(ref)
    if body is None or sha256(body) != expected:
        raise ValueError(f"gap_ab_object_hash_mismatch:{ref}")
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-report-ref", required=True)
    parser.add_argument("--gap-report-sha256", required=True)
    parser.add_argument("--base-fingerprint-sha256", required=True)
    parser.add_argument("--candidate-fingerprint-sha256", required=True)
    parser.add_argument("--compile-manifest-ref", required=True)
    parser.add_argument("--compile-manifest-sha256", required=True)
    parser.add_argument("--adapter-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url or not args.verifier_database_url:
        raise ValueError("gap_ab_database_url_missing")
    if args.database_url == args.verifier_database_url:
        raise ValueError("gap_ab_independent_verifier_required")

    identity = {"tenant_id": args.tenant_id, "username": "el3-ab-verifier", "role": "admin"}
    services = ReadOnlyServices(args.verifier_database_url, identity)
    gap = validate_gap_report(
        json.loads(_read(services, args.gap_report_ref, args.gap_report_sha256))
    )
    manifest = validate_compile_manifest(
        json.loads(
            _read(services, args.compile_manifest_ref, args.compile_manifest_sha256)
        )
    )
    targets = {item["fingerprint_sha256"]: item["fingerprint"] for item in gap["targets"]}
    if (
        args.base_fingerprint_sha256 not in targets
        or args.candidate_fingerprint_sha256 not in targets
    ):
        raise ValueError("gap_ab_target_missing")

    def arm(digest: str, candidate: bool = False):
        outcomes = [
            next(
                outcome
                for outcome in task["outcomes"]
                if outcome["target_fingerprint_sha256"] == digest
            )
            for task in gap["tasks"]
        ]
        transcripts = {
            outcome["transcript_ref"]: json.loads(
                _read(services, outcome["transcript_ref"], outcome["transcript_sha256"])
            )
            for outcome in outcomes
        }
        values = {
            "gap_report_ref": args.gap_report_ref,
            "gap_report_sha256": args.gap_report_sha256,
        }
        if candidate:
            return candidate_arm_from_gap(
                gap, digest, transcripts, adapter_id=args.adapter_id, **values
            )
        return base_arm_from_gap(gap, digest, transcripts, **values)

    report = build_migration_report(
        tenant_id=args.tenant_id,
        target_fingerprint=targets[args.base_fingerprint_sha256],
        learning_source={
            "kind": "compile_manifest",
            "ref": args.compile_manifest_ref,
            "sha256": args.compile_manifest_sha256,
            "value": manifest,
        },
        arms=[arm(args.base_fingerprint_sha256), arm(args.candidate_fingerprint_sha256, True)],
        policy={
            "version": "model-migration@1",
            "min_pass_rate": 1.0,
            "min_improvement": 0.01,
            "max_p95_regression_ratio": 1.2,
            "max_training_cost": 1.0,
        },
    )
    s3 = S3Utils()
    published = publish_migration_report(S3EvidenceStore(s3.bucket, s3.client), report)
    checked = default_verifiers().get("verify_model_migration", 1).handler(
        {"parameters": published}, identity, {}, services
    )
    if checked.status != "passed":
        raise RuntimeError(f"gap_ab_verification_failed:{checked.error_code}")
    print(json.dumps({**published, "decision": report["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()
