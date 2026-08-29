import json

import pytest

from harness.release_policy import (
    DEFAULT_RELEASE_POLICY,
    evaluate_repeated_holdout,
    summarize_report_target,
    validate_release_decision,
)


def repetition(normal_passed: int, critical_passed: int = 2, latency: float = 100.0):
    return {
        "normal": {"required": 100, "passed": normal_passed},
        "critical": {"required": 2, "passed": critical_passed},
        "p95_latency_ms": latency,
    }


def test_tiered_policy_requires_three_repetitions_and_critical_success():
    base = [repetition(80) for _ in range(3)]
    candidate = [repetition(92) for _ in range(3)]
    assert evaluate_repeated_holdout(base, candidate)["status"] == "GO"
    assert (
        evaluate_repeated_holdout(base[:2], candidate[:2])["reason"] == "repetitions_insufficient"
    )
    assert (
        evaluate_repeated_holdout(base, [repetition(92), repetition(92), repetition(92, 1)])[
            "status"
        ]
        == "NO-GO"
    )


def test_tiered_policy_rejects_result_chosen_by_one_lucky_run():
    base = [repetition(80) for _ in range(3)]
    candidate = [repetition(99), repetition(80), repetition(99)]
    assert evaluate_repeated_holdout(base, candidate)["status"] == "NO-GO"


def test_tiered_policy_allows_candidate_to_repair_base_critical_failures():
    base = [repetition(80, critical_passed=1) for _ in range(3)]
    candidate = [repetition(92) for _ in range(3)]

    assert evaluate_repeated_holdout(base, candidate)["status"] == "GO"


def test_tiered_policy_rejects_invalid_policy():
    with pytest.raises(ValueError, match="release_policy_fields_invalid"):
        evaluate_repeated_holdout([], [], {"normal_min_pass_rate": 0.9})


def test_release_decision_is_reproducible_from_repetitions():
    report = {
        "tasks": [
            {
                "outcomes": [
                    {
                        "target_fingerprint_sha256": "candidate",
                        "state": "succeeded",
                        "transcript_ref": "trial.json",
                    }
                ]
            }
        ]
    }
    metric = summarize_report_target(
        report,
        "candidate",
        lambda _ref: json.dumps({"latency_ms": 12}).encode(),
        critical_passed=2,
    )
    base = [repetition(40) for _ in range(3)]
    candidate = [{**metric, "normal": {"required": 1, "passed": 1}} for _ in range(3)]
    decision = {
        "schema_version": "release_decision.v1",
        "tenant_id": "acme",
        "policy": DEFAULT_RELEASE_POLICY,
        "base_fingerprint_sha256": "a" * 64,
        "candidate_fingerprint_sha256": "b" * 64,
        "reports": [{"ref": f"report-{i}", "sha256": str(i) * 64} for i in range(3)],
        "base_repetitions": base,
        "candidate_repetitions": candidate,
        "result": evaluate_repeated_holdout(base, candidate),
    }
    assert validate_release_decision(decision)["result"]["status"] == "GO"
