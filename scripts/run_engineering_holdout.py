#!/usr/bin/env python3
"""Run three EC3 holdout repetitions after a passing calibration."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import Counter
from pathlib import Path

import torch
from run_engineering_judge import PROMPT, _judgment, _model_digest
from transformers import AutoModelForCausalLM, AutoTokenizer

from core.evidence import canonical_bytes, sha256
from harness.engineering_judge import replay_calibration, validate_judgment
from harness.engineering_suite import _group, build_suite
from rag.answering import GroundedAnswering


def _candidate(case: dict) -> dict:
    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = None
    rows = [
        {
            "context_type": "document",
            "document_id": case["group_id"],
            "document_version": "engineering-candidate-v1",
            "chunk_id": item["chunk_id"],
            "text": item["text"],
        }
        for item in case["evidence"]
    ]
    result = answering.respond(case["query"], rows, "")
    return {
        "answer": result["answer"],
        "answer_status": result["answer_status"],
        "citations": [
            {"chunk_id": item["chunk_id"], "quote": item["quote"]} for item in result["citations"]
        ],
    }


def replay_holdout(report: dict, calibration: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if sha256(canonical_bytes(body)) != report.get("report_sha256"):
        raise ValueError("holdout_report_hash_mismatch")
    suite = build_suite()
    cases = [case for index in range(100) for case in _group("candidate_holdout_v2", index + 1000)]
    by_id = {case["case_id"]: case for case in cases}
    policy = suite["descriptor"]["policy"]
    for repetition in report["repetitions"]:
        if len(repetition["rows"]) != len(cases):
            raise ValueError("holdout_row_count")
        statuses = Counter()
        critical_failures = 0
        for row in repetition["rows"]:
            source = by_id[row["case_id"]]
            candidate = _candidate(source)
            if candidate != row["candidate_response"]:
                raise ValueError("holdout_candidate_replay_mismatch")
            result = validate_judgment({**source, "candidate_response": candidate}, row["raw"])
            if any(result[key] != row[key] for key in ("status", "judgment", "error")):
                raise ValueError("holdout_judgment_replay_mismatch")
            statuses[result["status"]] += 1
            critical_failures += (
                result["status"] != "pass" and source["category"] in policy["critical_categories"]
            )
        expected_statuses = {key: statuses[key] for key in ("pass", "fail", "uncertain", "invalid")}
        if (
            repetition["statuses"] != expected_statuses
            or repetition["pass_rate"] != statuses["pass"] / len(cases)
            or repetition["critical_failures"] != critical_failures
        ):
            raise ValueError("holdout_metrics_replay_mismatch")
    gates = {
        "repetitions": len(report["repetitions"]) == policy["repetitions"],
        "candidate_pass": all(
            item["pass_rate"] >= policy["candidate_pass_min"] for item in report["repetitions"]
        ),
        "candidate_delta": all(
            item["pass_rate"] - calibration["metrics"]["positive_acceptance"]["rate"]
            >= policy["candidate_pass_delta_min"]
            for item in report["repetitions"]
        ),
        "critical_new_failures": all(
            item["critical_failures"] <= policy["candidate_critical_new_failures_max"]
            for item in report["repetitions"]
        ),
        "invalid": all(
            item["statuses"]["invalid"] <= policy["invalid_max"] for item in report["repetitions"]
        ),
    }
    if report["gates"] != gates or report["decision"] != (
        "PASS" if all(gates.values()) else "NO_GO"
    ):
        raise ValueError("holdout_decision_replay_mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--calibration-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.audit_output.exists():
        raise SystemExit("judge_output_exists")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if (
        head != args.candidate_sha
        or subprocess.check_output(["git", "diff", "--name-only", "--", "src"], text=True).strip()
    ):
        raise SystemExit("candidate_source_not_frozen")
    calibration = json.loads(args.calibration_report.read_text())
    replay_calibration(calibration, calibration["report_sha256"])
    if calibration["calibration_decision"] != "PASS":
        raise SystemExit("judge_calibration_not_passed")

    suite = build_suite()
    cases = [case for index in range(100) for case in _group("candidate_holdout_v2", index + 1000)]
    holdout_descriptor = {
        "version": "engineering-candidate-v2-holdout",
        "generator": "structured-facts-v1",
        "offset": 1000,
        "count": len(cases),
        "cases_sha256": sha256(canonical_bytes(cases)),
    }
    model_digest = _model_digest(args.model_dir)
    if (
        calibration["suite"]["suite_sha256"] != suite["suite_sha256"]
        or calibration["model"]["provider_version"] != model_digest
        or calibration["prompt"] != PROMPT
    ):
        raise SystemExit("judge_identity_changed")
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model_dir, local_files_only=True, dtype=torch.float16, device_map="cuda"
    ).eval()
    model.generation_config.temperature = None
    model.generation_config.top_p = None
    model.generation_config.top_k = None
    torch.manual_seed(0)
    started = time.time()
    calls = []
    repetitions = []
    for repetition in range(1, suite["descriptor"]["policy"]["repetitions"] + 1):
        rows = []
        for number, source_case in enumerate(cases, 1):
            case = {**source_case, "candidate_response": _candidate(source_case)}
            payload = {key: case[key] for key in ("query", "evidence", "candidate_response")}
            rendered = tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = tokenizer(rendered, return_tensors="pt").to(model.device)
            call_started = time.perf_counter()
            with torch.inference_mode():
                generated = model.generate(**inputs, max_new_tokens=4, do_sample=False)
            output_tokens = generated.shape[1] - inputs.input_ids.shape[1]
            raw_model_output = tokenizer.decode(
                generated[0, inputs.input_ids.shape[1] :], skip_special_tokens=True
            ).strip()
            raw = _judgment(case, raw_model_output.upper())
            result = validate_judgment(case, raw)
            rows.append(
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "candidate_response": case["candidate_response"],
                    "raw": raw,
                    **result,
                }
            )
            calls.append(
                {
                    "repetition": repetition,
                    "case_id": case["case_id"],
                    "raw_model_output": raw_model_output,
                    "input_tokens": int(inputs.input_ids.shape[1]),
                    "output_tokens": int(output_tokens),
                    "latency_ms": (time.perf_counter() - call_started) * 1000,
                }
            )
            if number % 20 == 0:
                print(f"holdout {repetition}/3 {number}/{len(cases)}")
        counts = Counter(row["status"] for row in rows)
        repetitions.append(
            {
                "repetition": repetition,
                "rows": rows,
                "statuses": {key: counts[key] for key in ("pass", "fail", "uncertain", "invalid")},
                "pass_rate": counts["pass"] / len(rows),
                "critical_failures": sum(
                    row["status"] != "pass"
                    and row["category"] in suite["descriptor"]["policy"]["critical_categories"]
                    for row in rows
                ),
            }
        )

    policy = suite["descriptor"]["policy"]
    gates = {
        "repetitions": len(repetitions) == policy["repetitions"],
        "candidate_pass": all(
            item["pass_rate"] >= policy["candidate_pass_min"] for item in repetitions
        ),
        "candidate_delta": all(
            item["pass_rate"] - calibration["metrics"]["positive_acceptance"]["rate"]
            >= policy["candidate_pass_delta_min"]
            for item in repetitions
        ),
        "critical_new_failures": all(
            item["critical_failures"] <= policy["candidate_critical_new_failures_max"]
            for item in repetitions
        ),
        "invalid": all(
            item["statuses"]["invalid"] <= policy["invalid_max"] for item in repetitions
        ),
    }
    body = {
        "schema_version": "engineering_judge_holdout.v1",
        "candidate_sha": args.candidate_sha,
        "suite_sha256": suite["suite_sha256"],
        "holdout_descriptor": holdout_descriptor,
        "holdout_sha256": sha256(canonical_bytes(holdout_descriptor)),
        "calibration_report_sha256": calibration["report_sha256"],
        "model_sha256": model_digest,
        "prompt_sha256": sha256(PROMPT.encode()),
        "human_reviewed": False,
        "judge_only": True,
        "independent_semantic_verification": False,
        "training_allowed": False,
        "repetitions": repetitions,
        "gates": gates,
        "decision": "PASS" if all(gates.values()) else "NO_GO",
        "limitations": "Public correlated synthetic templates; no human calibration or business data.",
    }
    report = {**body, "report_sha256": sha256(canonical_bytes(body))}
    audit = {
        "schema_version": "engineering_judge_holdout_audit.v1",
        "candidate_sha": args.candidate_sha,
        "local_only": True,
        "cloud_egress": False,
        "calls": calls,
        "totals": {
            "calls": len(calls),
            "input_tokens": sum(item["input_tokens"] for item in calls),
            "output_tokens": sum(item["output_tokens"] for item in calls),
            "wall_time_seconds": time.time() - started,
        },
        "report_sha256": report["report_sha256"],
    }
    replay_holdout(report, calibration)
    args.output.write_bytes(canonical_bytes(report))
    args.audit_output.write_bytes(canonical_bytes(audit))
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "report_sha256": report["report_sha256"],
                "audit_sha256": sha256(canonical_bytes(audit)),
                "repetitions": [
                    {
                        "pass_rate": item["pass_rate"],
                        "statuses": item["statuses"],
                        "critical_failures": item["critical_failures"],
                    }
                    for item in repetitions
                ],
                "gates": gates,
                "totals": audit["totals"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
