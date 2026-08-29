"""Small, immutable release policy for repeated model evaluations."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

DEFAULT_RELEASE_POLICY = {
    "schema_version": "release_policy.v1",
    "version": "capability-tiered@1",
    "normal_min_pass_rate": 0.90,
    "normal_min_improvement": 0.01,
    "critical_min_pass_rate": 1.0,
    "min_repetitions": 3,
    "max_p95_regression_ratio": 1.20,
}


def validate_release_policy(policy: dict[str, Any]) -> dict[str, Any]:
    required = set(DEFAULT_RELEASE_POLICY)
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError("release_policy_fields_invalid")
    if policy["schema_version"] != DEFAULT_RELEASE_POLICY["schema_version"]:
        raise ValueError("release_policy_version_invalid")
    if not isinstance(policy["version"], str) or not policy["version"]:
        raise ValueError("release_policy_version_invalid")
    if (
        type(policy["normal_min_pass_rate"]) not in {int, float}
        or not 0 <= policy["normal_min_pass_rate"] <= 1
        or type(policy["normal_min_improvement"]) not in {int, float}
        or not 0 <= policy["normal_min_improvement"] <= 1
        or policy["normal_min_improvement"] > policy["normal_min_pass_rate"]
        or policy["critical_min_pass_rate"] != 1.0
        or type(policy["min_repetitions"]) is not int
        or policy["min_repetitions"] < 3
        or type(policy["max_p95_regression_ratio"]) not in {int, float}
        or policy["max_p95_regression_ratio"] < 1
    ):
        raise ValueError("release_policy_values_invalid")
    return deepcopy(policy)


def evaluate_repeated_holdout(
    base_repetitions: list[dict[str, Any]],
    candidate_repetitions: list[dict[str, Any]],
    policy: dict[str, Any] = DEFAULT_RELEASE_POLICY,
) -> dict[str, Any]:
    """Evaluate repeated normal capability and critical hard-gate metrics."""
    policy = validate_release_policy(policy)
    if len(base_repetitions) != len(candidate_repetitions):
        raise ValueError("release_repetition_count_mismatch")
    if len(base_repetitions) < policy["min_repetitions"]:
        return {"status": "BLOCKED", "reason": "repetitions_insufficient"}
    required = {"normal", "critical", "p95_latency_ms"}
    if any(set(item) != required for item in base_repetitions + candidate_repetitions):
        raise ValueError("release_repetition_fields_invalid")

    def rate(item: dict[str, Any], key: str) -> float:
        metric = item[key]
        if (
            not isinstance(metric, dict)
            or set(metric) != {"required", "passed"}
            or type(metric["required"]) is not int
            or metric["required"] <= 0
            or type(metric["passed"]) is not int
            or not 0 <= metric["passed"] <= metric["required"]
        ):
            raise ValueError("release_repetition_metric_invalid")
        return metric["passed"] / metric["required"]

    base_normal = [rate(item, "normal") for item in base_repetitions]
    candidate_normal = [rate(item, "normal") for item in candidate_repetitions]
    candidate_critical = [rate(item, "critical") for item in candidate_repetitions]
    if any(
        type(item["p95_latency_ms"]) not in {int, float} or item["p95_latency_ms"] < 0
        for item in base_repetitions + candidate_repetitions
    ):
        raise ValueError("release_repetition_latency_invalid")
    base_mean = sum(base_normal) / len(base_normal)
    candidate_mean = sum(candidate_normal) / len(candidate_normal)
    latency_limit = (
        max(item["p95_latency_ms"] for item in base_repetitions)
        * policy["max_p95_regression_ratio"]
    )
    critical_passed = min(candidate_critical) == policy["critical_min_pass_rate"]
    normal_passed = min(candidate_normal) >= policy["normal_min_pass_rate"]
    improvement_passed = candidate_mean - base_mean >= policy["normal_min_improvement"]
    latency_passed = max(item["p95_latency_ms"] for item in candidate_repetitions) <= latency_limit
    status = (
        "GO"
        if all((critical_passed, normal_passed, improvement_passed, latency_passed))
        else "NO-GO"
    )
    return {
        "status": status,
        "reason": "tiered_policy_passed" if status == "GO" else "tiered_policy_failed",
        "repetitions": len(base_repetitions),
        "base_normal_pass_rate": base_mean,
        "candidate_normal_pass_rate": candidate_mean,
        "candidate_min_normal_pass_rate": min(candidate_normal),
        "critical_passed": critical_passed,
        "improvement_passed": improvement_passed,
        "latency_passed": latency_passed,
    }


def summarize_report_target(
    report: dict[str, Any],
    target_digest: str,
    transcript_body: Any,
    *,
    critical_passed: int,
) -> dict[str, Any]:
    """Build one policy repetition from verified gap-report outcomes."""
    outcomes = [
        outcome
        for task in report["tasks"]
        for outcome in task["outcomes"]
        if outcome["target_fingerprint_sha256"] == target_digest
    ]
    latencies = sorted(
        float(json.loads(transcript_body(item["transcript_ref"]))["latency_ms"])
        for item in outcomes
    )
    return {
        "normal": {
            "required": len(outcomes),
            "passed": sum(item["state"] == "succeeded" for item in outcomes),
        },
        "critical": {"required": 2, "passed": critical_passed},
        "p95_latency_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
    }


def validate_release_decision(decision: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "tenant_id",
        "policy",
        "base_fingerprint_sha256",
        "candidate_fingerprint_sha256",
        "reports",
        "base_repetitions",
        "candidate_repetitions",
        "result",
    }
    if not isinstance(decision, dict) or set(decision) != required:
        raise ValueError("release_decision_fields_invalid")
    if decision["schema_version"] != "release_decision.v1" or not decision["tenant_id"]:
        raise ValueError("release_decision_schema_invalid")
    validate_release_policy(decision["policy"])
    reports = decision["reports"]
    if len(reports) < decision["policy"]["min_repetitions"] or any(
        not isinstance(item, dict) or set(item) != {"ref", "sha256"} for item in reports
    ):
        raise ValueError("release_decision_reports_invalid")
    expected = evaluate_repeated_holdout(
        decision["base_repetitions"],
        decision["candidate_repetitions"],
        decision["policy"],
    )
    if decision["result"] != expected:
        raise ValueError("release_decision_result_invalid")
    return deepcopy(decision)


def verify_adapter_for_release(
    database_url: str,
    verifier_database_url: str,
    identity: dict[str, str],
    adapter_id: str,
    decision_ref: str,
    decision_sha256: str,
) -> None:
    """Bind an independently replayed GO decision to one exact adapter artifact."""
    from core.verifiers import ReadOnlyServices, default_verifiers
    from storage.audit import AuditLog
    from storage.postgres import PostgresDatabase

    services = ReadOnlyServices(verifier_database_url, identity)
    checked = (
        default_verifiers()
        .get("verify_release_decision", 1)
        .handler(
            {
                "parameters": {
                    "decision_ref": decision_ref,
                    "decision_sha256": decision_sha256,
                }
            },
            identity,
            {},
            services,
        )
    )
    if checked.status != "passed" or checked.summary.get("status") != "GO":
        raise ValueError("adapter_release_decision_unverified")
    decision = validate_release_decision(json.loads(services.object_body(decision_ref)))
    report = json.loads(services.object_body(decision["reports"][0]["ref"]))
    fingerprint = next(
        item["fingerprint"]
        for item in report["targets"]
        if item["fingerprint_sha256"] == decision["candidate_fingerprint_sha256"]
    )
    database = PostgresDatabase(database_url)
    with database.transaction(identity) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT a.state, a.artifact_sha256, a.base_model_digest, a.tokenizer_digest, "
                "a.config_json, a.safety_scan_json, s.state AS snapshot_state "
                "FROM adapter_manifests a JOIN training_snapshots s "
                "ON s.snapshot_id = a.snapshot_id "
                "WHERE a.adapter_id = %s FOR UPDATE",
                (adapter_id,),
            )
            row = cursor.fetchone()
            descriptor = {"ref": decision_ref, "sha256": decision_sha256}
            if (
                row
                and row["state"] == "verified"
                and row["config_json"].get("release_decision") == descriptor
            ):
                return
            if (
                row is None
                or row["state"] != "candidate"
                or row["snapshot_state"] != "approved"
                or row["safety_scan_json"].get("passed") is not True
                or row["artifact_sha256"] != fingerprint["adapter_sha256"]
                or row["base_model_digest"] != fingerprint["model_sha256"]
                or row["tokenizer_digest"] != fingerprint["tokenizer_sha256"]
            ):
                raise ValueError("adapter_release_identity_mismatch")
            config = {**row["config_json"], "release_decision": descriptor}
            cursor.execute(
                "UPDATE adapter_manifests SET state = 'verified', config_json = %s::jsonb "
                "WHERE adapter_id = %s",
                (json.dumps(config, sort_keys=True), adapter_id),
            )
    AuditLog(database_url).record(
        identity,
        "adapter.release_verified",
        "adapter",
        resource_id=adapter_id,
        metadata=descriptor,
    )
