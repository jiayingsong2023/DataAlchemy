"""Merge disjoint, verified gap reports produced with one frozen evaluation contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.compiler import validate_gap_report
from harness.evaluation import build_gap_report
from scripts.rerollout_task_bundles import _sha256
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-ref", action="append", required=True)
    parser.add_argument("--report-sha256", action="append", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    if len(args.report_ref) != len(args.report_sha256) or not args.database_url:
        raise ValueError("gap_merge_inputs_invalid")

    store = S3Utils()
    reports = []
    for ref, expected in zip(args.report_ref, args.report_sha256, strict=True):
        body = store.get_object_body(ref)
        if body is None or _sha256(body) != expected:
            raise ValueError(f"gap_merge_object_invalid:{ref}")
        reports.append(validate_gap_report(json.loads(body)))
    first = reports[0]
    contract = (
        first["targets"],
        first["generation_policy_sha256"],
        first["verifier"],
    )
    if any(
        (item["targets"], item["generation_policy_sha256"], item["verifier"]) != contract
        for item in reports[1:]
    ):
        raise ValueError("gap_merge_contract_mismatch")
    tasks = [task for report in reports for task in report["tasks"]]
    if len({task["task_bundle_id"] for task in tasks}) != len(tasks):
        raise ValueError("gap_merge_task_overlap")
    merged = build_gap_report(
        [item["fingerprint"] for item in first["targets"]],
        [outcome for task in tasks for outcome in task["outcomes"]],
        generation_policy_sha256=first["generation_policy_sha256"],
        verifier_contract_digest=first["verifier"]["contract_digest"],
    )
    body = canonical_bytes(merged)
    digest = _sha256(body)
    ref = f"{args.output_prefix.rstrip('/')}/{digest}.json"
    if not store.put_object(ref, body, "application/json"):
        raise RuntimeError("gap_merge_write_failed")
    verified = (
        default_verifiers()
        .get("verify_gap_report", 1)
        .handler(
            {
                "parameters": {
                    "report_ref": ref,
                    "report_sha256": digest,
                    "generation_policy_sha256": first["generation_policy_sha256"],
                    "verifier_contract_digest": first["verifier"]["contract_digest"],
                }
            },
            {"tenant_id": args.tenant_id},
            {},
            ReadOnlyServices(
                args.database_url,
                {"tenant_id": args.tenant_id, "username": "gap-merger", "role": "admin"},
            ),
        )
    )
    if verified.status != "passed":
        raise RuntimeError(f"gap_merge_verification_failed:{verified.error_code}")
    print(json.dumps({"report_ref": ref, "report_sha256": digest, **merged["metrics"]}))


if __name__ == "__main__":
    main()
