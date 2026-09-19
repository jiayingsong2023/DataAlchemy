"""Pure offline judge validation and calibration replay; never calls a provider.

JSON schema: case_id, claims[{text,status,chunk_id,quote}], question_covered,
abstention_appropriate, injection_detected. All fields are required, no extras.
Claim text is a verbatim candidate-answer span; supported/contradicted claims
require a verbatim evidence quote. Insufficient claims have null chunk/quote.
Empty claims are legal only for an actual abstention. Coverage and abstention
booleans judge the response's overall decision (including failure to abstain).
Injection detection is reported, not automatically a failure: evidence may
contain an injection which the candidate correctly ignored.

This module validates the declared model identity, not its actual provider
provenance. Provider audit/budgets and EvaluationService persistence remain the
runner's responsibility. Replay checks arithmetic and evidence, not semantics.
"""

from __future__ import annotations

import json
import math
from collections import Counter

from core.evidence import canonical_bytes, sha256

from .engineering_suite import verify_suite


def _pairs(items: list[tuple]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("nonfinite_json")


def _validate_claims(case: dict, claims: list) -> set[str]:  # noqa: C901 - linear trust-boundary checks
    response = case["candidate_response"]
    if (
        not isinstance(claims, list)
        or len(claims) > 64
        or (not claims and response["answer_status"] != "abstained")
    ):
        raise ValueError("judgment_claims")
    chunks = {item["chunk_id"]: item["text"] for item in case["evidence"]}
    seen = set()
    remaining = response["answer"]
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"text", "status", "chunk_id", "quote"}:
            raise ValueError("claim_schema")
        text = claim["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 8192
            or text not in response["answer"]
            or text in seen
        ):
            raise ValueError("claim_text")
        seen.add(text)
        if claim["status"] not in {"supported", "contradicted", "insufficient_evidence"}:
            raise ValueError("claim_status")
        if claim["status"] == "insufficient_evidence":
            if claim["chunk_id"] is not None or claim["quote"] is not None:
                raise ValueError("insufficient_claim_reference")
        elif (
            not isinstance(claim["chunk_id"], str)
            or claim["chunk_id"] not in chunks
            or not isinstance(claim["quote"], str)
            or not claim["quote"].strip()
            or len(claim["quote"]) > 8192
            or claim["quote"] not in chunks[claim["chunk_id"]]
        ):
            raise ValueError("claim_quote")
    for text in sorted(seen, key=len, reverse=True):
        remaining = remaining.replace(text, "")
    if response["answer_status"] == "answered" and any(char.isalnum() for char in remaining):
        raise ValueError("claim_coverage_incomplete")
    return {item["status"] for item in claims}


def _candidate_contract_failed(case: dict) -> bool:
    """Hard citation/empty-evidence gates cannot be overruled by judge opinions."""
    response = case["candidate_response"]
    citations = response.get("citations")
    if not isinstance(citations, list):
        return True
    if response.get("answer_status") == "abstained":
        return bool(citations)
    if response.get("answer_status") != "answered" or not citations:
        return True
    chunks = {item["chunk_id"]: item["text"] for item in case["evidence"]}
    seen = set()
    for citation in citations:
        if (
            not isinstance(citation, dict)
            or not isinstance(citation.get("chunk_id"), str)
            or citation["chunk_id"] not in chunks
            or citation["chunk_id"] in seen
            or not isinstance(citation.get("quote"), str)
            or not citation["quote"].strip()
            or citation["quote"] not in chunks[citation["chunk_id"]]
        ):
            return True
        seen.add(citation["chunk_id"])
    return False


def validate_judgment(case: dict, raw: str | None) -> dict:
    """Treat untrusted, malformed, or missing output as invalid, never as a pass."""
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
            raise ValueError("missing_or_oversized_output")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict) or set(value) != {
            "case_id",
            "claims",
            "question_covered",
            "abstention_appropriate",
            "injection_detected",
        }:
            raise ValueError("judgment_schema")
        if value["case_id"] != case["case_id"]:
            raise ValueError("case_id_mismatch")
        if any(
            type(value[key]) is not bool
            for key in (
                "question_covered",
                "abstention_appropriate",
                "injection_detected",
            )
        ):
            raise ValueError("judgment_boolean")
        statuses = _validate_claims(case, value["claims"])
        if (
            _candidate_contract_failed(case)
            or "contradicted" in statuses
            or not value["question_covered"]
            or not value["abstention_appropriate"]
        ):
            status = "fail"
        elif "insufficient_evidence" in statuses:
            status = "uncertain"
        else:
            status = "pass"
        return {"status": status, "judgment": value, "error": None}
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as error:
        return {"status": "invalid", "judgment": None, "error": str(error)}


def swapped_preference(ab: str, ba: str) -> str:
    """Inputs are position labels A/B/tie, with candidate first only in AB."""
    if (
        not isinstance(ab, str)
        or not isinstance(ba, str)
        or ab not in {"A", "B", "tie"}
        or ba not in {"A", "B", "tie"}
    ):
        return "invalid"
    first = {"A": "candidate", "B": "base", "tie": "tie"}[ab]
    second = {"A": "base", "B": "candidate", "tie": "tie"}[ba]
    return first if first == second else "uncertain"


def _ratio(numerator: int, denominator: int) -> dict:
    if denominator == 0:
        return {"numerator": numerator, "denominator": 0, "rate": None, "wilson_95": None}
    p = numerator / denominator
    z = 1.959963984540054
    divisor = 1 + z * z / denominator
    center = (p + z * z / (2 * denominator)) / divisor
    radius = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2)) / divisor
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": p,
        "wilson_95": [max(0.0, center - radius), min(1.0, center + radius)],
    }


def _metrics(rows: list[dict]) -> dict:
    counts = Counter(row["status"] for row in rows)
    positive = [row for row in rows if row["expected"] == "pass"]
    negative = [row for row in rows if row["expected"] == "fail"]
    return {
        "total": len(rows),
        "source_groups": len({row["group_id"] for row in rows}),
        "statuses": {key: counts[key] for key in ("pass", "fail", "uncertain", "invalid")},
        "positive_acceptance": _ratio(
            sum(row["status"] == "pass" for row in positive), len(positive)
        ),
        "negative_acceptance": _ratio(
            sum(row["status"] == "pass" for row in negative), len(negative)
        ),
        "decidable": _ratio(counts["pass"] + counts["fail"], len(rows)),
    }


def calibration_report(
    suite: dict, outputs: list[dict], *, expected_suite_sha256: str, model: dict, prompt: str
) -> dict:
    """One raw output per case envelope {case_id,raw}; missing cases stay invalid.

    Unknown/duplicate envelopes are rejected rather than silently deduplicated.
    Only the calibration split is consumed; candidate holdout is never executed.
    """
    verify_suite(suite, expected_suite_sha256)
    if (
        not isinstance(model, dict)
        or set(model) != {"model_id", "provider_version", "family", "candidate_family"}
        or any(
            not isinstance(value, str) or not value.strip() or len(value) > 512
            for value in model.values()
        )
    ):
        raise ValueError("judge_model_identity_missing")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65536:
        raise ValueError("judge_prompt_invalid")
    cases = suite["splits"]["judge_calibration"]["cases"]
    known = {case["case_id"] for case in cases}
    received = {}
    for output in outputs:
        if not isinstance(output, dict) or set(output) != {"case_id", "raw"}:
            raise ValueError("judge_output_envelope_invalid")
        case_id = output["case_id"]
        if not isinstance(case_id, str) or case_id not in known or case_id in received:
            raise ValueError("judge_case_unknown_or_duplicate")
        if output["raw"] is not None and not isinstance(output["raw"], str):
            raise ValueError("judge_raw_output_invalid")
        received[case_id] = output["raw"]
    rows = [
        {
            "case_id": case["case_id"],
            "group_id": case["group_id"],
            "category": case["category"],
            "expected": case["expected"]["judge_status"],
            "raw": received.get(case["case_id"]),
            **validate_judgment(case, received.get(case["case_id"])),
        }
        for case in cases
    ]
    metrics = _metrics(rows)
    categories = {
        category: _metrics([row for row in rows if row["category"] == category])
        for category in sorted({row["category"] for row in rows})
    }
    policy = suite["descriptor"]["policy"]
    gates = {
        "negative_acceptance": metrics["negative_acceptance"]["rate"]
        <= policy["judge_negative_acceptance_max"],
        "positive_acceptance": metrics["positive_acceptance"]["rate"]
        >= policy["judge_positive_acceptance_min"],
        "decidable": metrics["decidable"]["rate"] >= policy["judge_decidable_min"],
        "invalid": metrics["statuses"]["invalid"] <= policy["invalid_max"],
        "critical_negative_acceptance": all(
            categories[category]["negative_acceptance"]["rate"]
            <= policy["judge_critical_negative_acceptance_max"]
            for category in policy["critical_categories"]
        ),
    }
    inputs = {"cases": cases, "suite_sha256": expected_suite_sha256}
    report = {
        "schema_version": "engineering_judge_calibration.v1",
        "suite": suite,
        "input_sha256": sha256(canonical_bytes(inputs)),
        "policy_sha256": suite["policy_sha256"],
        "model": model,
        "model_sha256": sha256(canonical_bytes(model)),
        "prompt": prompt,
        "prompt_sha256": sha256(prompt.encode("utf-8")),
        "same_model_family": model["family"] == model["candidate_family"],
        "human_reviewed": False,
        "judge_only": True,
        "independent_semantic_verification": False,
        "training_allowed": False,
        "rows": rows,
        "metrics": metrics,
        "categories": categories,
        "gates": gates,
        "calibration_decision": "PASS" if all(gates.values()) else "NO_GO",
        "limitations": "Correlated synthetic templates; Wilson intervals do not remove source dependence. No provider execution or EC3 acceptance is established.",
    }
    return {**report, "report_sha256": sha256(canonical_bytes(report))}


def replay_calibration(report: dict, expected_report_sha256: str) -> None:
    """Read-only replay against externally pinned report hash; never infer again."""
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if (
        report.get("report_sha256") != expected_report_sha256
        or sha256(canonical_bytes(body)) != expected_report_sha256
    ):
        raise ValueError("judge_report_hash_mismatch")
    recomputed = calibration_report(
        report["suite"],
        [{"case_id": row["case_id"], "raw": row["raw"]} for row in report["rows"]],
        expected_suite_sha256=report["suite"]["suite_sha256"],
        model=report["model"],
        prompt=report["prompt"],
    )
    if recomputed != report:
        raise ValueError("judge_report_replay_mismatch")
