"""Publish and independently verify a three-run tiered release decision."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.release_policy import (
    DEFAULT_RELEASE_POLICY,
    evaluate_repeated_holdout,
    summarize_report_target,
    validate_release_decision,
)
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-ref", action="append", required=True)
    parser.add_argument("--report-sha256", action="append", required=True)
    parser.add_argument("--candidate-fingerprint-sha256", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    parser.add_argument("--output-prefix", default="release/decisions")
    args = parser.parse_args()
    if not args.database_url or len(args.report_ref) != len(args.report_sha256):
        raise ValueError("release_decision_inputs_invalid")

    identity = {"tenant_id": args.tenant_id, "username": "release-verifier", "role": "reviewer"}
    services = ReadOnlyServices(args.database_url, identity)
    store = S3Utils()
    descriptors, base_metrics, candidate_metrics = [], [], []
    base_digest = None
    for ref, expected in zip(args.report_ref, args.report_sha256, strict=True):
        body = services.object_body(ref)
        if body is None or sha256(body) != expected:
            raise ValueError("release_report_hash_mismatch")
        report = json.loads(body)
        digests = {item["fingerprint_sha256"] for item in report["targets"]}
        if args.candidate_fingerprint_sha256 not in digests:
            raise ValueError("release_candidate_not_in_report")
        current_base = next(iter(digests - {args.candidate_fingerprint_sha256}))
        if base_digest not in {None, current_base}:
            raise ValueError("release_base_changed_between_repetitions")
        base_digest = current_base
        verified = default_verifiers().get("verify_gap_report", 1).handler(
            {
                "parameters": {
                    "report_ref": ref,
                    "report_sha256": expected,
                    "generation_policy_sha256": report["generation_policy_sha256"],
                    "verifier_contract_digest": report["verifier"]["contract_digest"],
                }
            },
            identity,
            {},
            services,
        )
        candidate_outcomes = [
            outcome
            for item in report["tasks"]
            for outcome in item["outcomes"]
            if outcome["target_fingerprint_sha256"] == args.candidate_fingerprint_sha256
        ]
        transcript_gate = verified.status == "passed" and all(
            default_verifiers()
            .get("verify_trial_transcript", 1)
            .handler(
                {"parameters": {"trial_id": outcome["trial_id"]}},
                identity,
                {},
                services,
            )
            .status
            == "passed"
            for outcome in candidate_outcomes
        )
        critical = int(verified.status == "passed") + int(transcript_gate)

        def transcript(transcript_ref: str) -> bytes:
            value = services.object_body(transcript_ref)
            if value is None:
                raise ValueError("release_transcript_missing")
            return value

        base_metrics.append(
            summarize_report_target(
                report, base_digest, transcript, critical_passed=critical
            )
        )
        candidate_metrics.append(
            summarize_report_target(
                report,
                args.candidate_fingerprint_sha256,
                transcript,
                critical_passed=critical,
            )
        )
        descriptors.append({"ref": ref, "sha256": expected})
    decision = validate_release_decision(
        {
            "schema_version": "release_decision.v1",
            "tenant_id": args.tenant_id,
            "policy": DEFAULT_RELEASE_POLICY,
            "base_fingerprint_sha256": base_digest,
            "candidate_fingerprint_sha256": args.candidate_fingerprint_sha256,
            "reports": descriptors,
            "base_repetitions": base_metrics,
            "candidate_repetitions": candidate_metrics,
            "result": evaluate_repeated_holdout(base_metrics, candidate_metrics),
        }
    )
    body = canonical_bytes(decision)
    digest = sha256(body)
    ref = f"{args.output_prefix.rstrip('/')}/sha256/{digest}.json"
    if not store.put_object(ref, body, "application/json"):
        raise RuntimeError("release_decision_write_failed")
    checked = default_verifiers().get("verify_release_decision", 1).handler(
        {"parameters": {"decision_ref": ref, "decision_sha256": digest}},
        identity,
        {},
        services,
    )
    if checked.status != "passed":
        raise RuntimeError(f"release_decision_verification_failed:{checked.error_code}")
    print(
        json.dumps(
            {"decision_ref": ref, "decision_sha256": digest, **checked.summary},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
