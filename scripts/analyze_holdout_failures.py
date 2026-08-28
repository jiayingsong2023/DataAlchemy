"""Publish a deterministic root-cause summary for candidate holdout failures."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes, sha256
from harness.compiler import validate_gap_report
from utils.s3_utils import S3Utils


def _read(store: S3Utils, ref: str, expected: str) -> dict[str, Any]:
    body = store.get_object_body(ref)
    if body is None or sha256(body) != expected:
        raise ValueError(f"holdout_failure_object_hash_mismatch:{ref}")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("holdout_failure_object_invalid")
    return value


def _category(transcript: dict[str, Any]) -> str:
    verifier = transcript.get("verifier", {})
    code = str(verifier.get("error_code") or "unknown").lower()
    if "citation" in code or "page" in code or "source" in code:
        return "citation_or_source"
    if "retriev" in code or "context" in code or "evidence" in code:
        return "retrieval_or_evidence"
    if "abstain" in code or transcript.get("status") == "abstained":
        return "abstention"
    if "answer" in code or "substring" in code or "expected" in code:
        return "answer_content"
    return "verifier_or_other"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-report-ref", required=True)
    parser.add_argument("--gap-report-sha256", required=True)
    parser.add_argument("--candidate-fingerprint-sha256", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output-prefix", default="annotations/holdout-failure-analysis")
    args = parser.parse_args()

    store = S3Utils()
    report = validate_gap_report(
        _read(store, args.gap_report_ref, args.gap_report_sha256)
    )
    targets = {item["fingerprint_sha256"] for item in report["targets"]}
    if args.candidate_fingerprint_sha256 not in targets:
        raise ValueError("holdout_failure_candidate_missing")
    failures = []
    for task in report["tasks"]:
        if task.get("split") != "evaluation_holdout":
            continue
        outcome = next(
            item
            for item in task["outcomes"]
            if item["target_fingerprint_sha256"] == args.candidate_fingerprint_sha256
        )
        if outcome["state"] != "failed":
            continue
        transcript = _read(store, outcome["transcript_ref"], outcome["transcript_sha256"])
        verifier = transcript.get("verifier", {})
        failures.append(
            {
                "case_id": task["case_id"],
                "task_bundle_id": task["task_bundle_id"],
                "trial_id": outcome["trial_id"],
                "category": _category(transcript),
                "failure_code": verifier.get("error_code"),
                "status": transcript.get("status"),
                "answer": transcript.get("answer", ""),
                "citation_count": len(transcript.get("citations", [])),
                "transcript_ref": outcome["transcript_ref"],
                "transcript_sha256": outcome["transcript_sha256"],
            }
        )
    categories = Counter(item["category"] for item in failures)
    codes = Counter(str(item["failure_code"] or "unknown") for item in failures)
    analysis = {
        "schema_version": "holdout_failure_analysis.v1",
        "tenant_id": args.tenant_id,
        "gap_report": {"ref": args.gap_report_ref, "sha256": args.gap_report_sha256},
        "candidate_fingerprint_sha256": args.candidate_fingerprint_sha256,
        "split": "evaluation_holdout",
        "failure_count": len(failures),
        "category_counts": dict(sorted(categories.items())),
        "failure_code_counts": dict(sorted(codes.items())),
        "failures": sorted(failures, key=lambda item: item["case_id"]),
    }
    body = canonical_bytes(analysis)
    digest = sha256(body)
    ref = f"{args.output_prefix.rstrip('/')}/{digest}.json"
    if not store.put_object(ref, body, "application/json"):
        raise RuntimeError("holdout_failure_analysis_write_failed")
    print(json.dumps({"analysis_ref": ref, "analysis_sha256": digest, **analysis}, sort_keys=True))


if __name__ == "__main__":
    main()
