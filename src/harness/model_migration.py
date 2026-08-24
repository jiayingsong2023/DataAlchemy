"""Controlled base/candidate comparison and model-migration decisions."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from core.evidence import EvidenceObjectStore, canonical_bytes, sha256
from harness.compiler import (
    validate_compile_decision,
    validate_compile_manifest,
    validate_gap_report,
)
from harness.evaluation import model_fingerprint_digest, validate_model_fingerprint
from harness.experience import _put_immutable

_HEX = frozenset("0123456789abcdef")


def _sha(value: Any, error: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(error)
    return value


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)])


def base_arm_from_gap(
    gap_report: dict[str, Any],
    target_fingerprint_sha256: str,
    transcripts: dict[str, dict[str, Any]],
    *,
    gap_report_ref: str,
    gap_report_sha256: str,
) -> dict[str, Any]:
    """Project one target's real re-rollout outcomes into the A arm."""
    report = validate_gap_report(gap_report)
    if sha256(canonical_bytes(report)) != gap_report_sha256:
        raise ValueError("migration_gap_report_hash_mismatch")
    target = next(
        (
            item
            for item in report["targets"]
            if item["fingerprint_sha256"] == target_fingerprint_sha256
        ),
        None,
    )
    if target is None:
        raise ValueError("migration_target_not_in_gap_report")
    outcomes = [
        next(
            item
            for item in task["outcomes"]
            if item["target_fingerprint_sha256"] == target_fingerprint_sha256
        )
        for task in report["tasks"]
    ]
    latencies = []
    for outcome in outcomes:
        transcript = transcripts.get(outcome.get("transcript_ref"))
        _sha(
            outcome.get("environment_initial_state_sha256"),
            "migration_environment_hash_invalid",
        )
        latency = transcript.get("latency_ms") if isinstance(transcript, dict) else None
        if (
            not isinstance(transcript, dict)
            or outcome.get("state") not in {"succeeded", "failed", "invalidated"}
            or sha256(canonical_bytes(transcript)) != outcome.get("transcript_sha256")
            or transcript.get("model_fingerprint") != target["fingerprint"]
            or transcript.get("generation_policy_sha256") != report["generation_policy_sha256"]
            or transcript.get("verifier", {}).get("contract_digest")
            != report["verifier"]["contract_digest"]
            or type(latency) not in {int, float}
            or not math.isfinite(latency)
            or latency < 0
        ):
            raise ValueError("migration_base_transcript_mismatch")
        if outcome["state"] in {"succeeded", "failed"}:
            latencies.append(float(latency))
    valid = sum(item["state"] in {"succeeded", "failed"} for item in outcomes)
    invalid = sum(item["state"] == "invalidated" for item in outcomes)
    succeeded = sum(item["state"] == "succeeded" for item in outcomes)
    return {
        "name": "base",
        "subject_type": "base",
        "subject_ref": target_fingerprint_sha256,
        "fingerprint_sha256": target_fingerprint_sha256,
        "evidence": {"kind": "gap_report", "ref": gap_report_ref, "sha256": gap_report_sha256},
        "task_bundle_ids": sorted(task["task_bundle_id"] for task in report["tasks"]),
        "environment_initial_state_sha256": sorted(
            {item["environment_initial_state_sha256"] for item in outcomes}
        ),
        "generation_policy_sha256": report["generation_policy_sha256"],
        "verifier_contract_digest": report["verifier"]["contract_digest"],
        "required_trials": len(outcomes),
        "valid_trials": valid,
        "invalid_trials": invalid,
        "metrics": {
            "pass_rate": succeeded / valid if valid else 0.0,
            "p95_latency_ms": _p95(latencies),
            "training_cost": 0.0,
        },
        "hard_gates": {"passed": valid == len(outcomes) and succeeded == valid},
    }


def _validate_arm(arm: dict[str, Any]) -> dict[str, Any]:  # noqa: C901 - fail-closed schema
    required = {
        "name",
        "subject_type",
        "subject_ref",
        "fingerprint_sha256",
        "evidence",
        "task_bundle_ids",
        "environment_initial_state_sha256",
        "generation_policy_sha256",
        "verifier_contract_digest",
        "required_trials",
        "valid_trials",
        "invalid_trials",
        "metrics",
        "hard_gates",
    }
    if not isinstance(arm, dict) or set(arm) != required:
        raise ValueError("migration_arm_fields_invalid")
    if arm["name"] not in {"base", "gap_sft", "full_sft"}:
        raise ValueError("migration_arm_name_invalid")
    if arm["subject_type"] not in {"base", "adapter"} or not arm["subject_ref"]:
        raise ValueError("migration_arm_subject_invalid")
    for key in (
        "fingerprint_sha256",
        "generation_policy_sha256",
        "verifier_contract_digest",
    ):
        _sha(arm[key], "migration_arm_hash_invalid")
    evidence = arm["evidence"]
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"kind", "ref", "sha256"}
        or evidence["kind"] not in {"gap_report", "evaluation"}
        or not evidence["ref"]
    ):
        raise ValueError("migration_arm_evidence_invalid")
    _sha(evidence["sha256"], "migration_arm_evidence_hash_invalid")
    for key in ("task_bundle_ids", "environment_initial_state_sha256"):
        if not isinstance(arm[key], list) or not arm[key] or len(set(arm[key])) != len(arm[key]):
            raise ValueError("migration_arm_alignment_invalid")
    if (
        any(
            type(arm[key]) is not int or arm[key] < 0
            for key in ("required_trials", "valid_trials", "invalid_trials")
        )
        or arm["valid_trials"] + arm["invalid_trials"] != arm["required_trials"]
    ):
        raise ValueError("migration_arm_trials_invalid")
    metrics = arm["metrics"]
    if set(metrics) != {"pass_rate", "p95_latency_ms", "training_cost"} or any(
        value is not None and type(value) not in {int, float} for value in metrics.values()
    ):
        raise ValueError("migration_arm_metrics_invalid")
    if (
        not 0 <= metrics["pass_rate"] <= 1
        or metrics["p95_latency_ms"] < 0
        or (metrics["training_cost"] is not None and metrics["training_cost"] < 0)
    ):
        raise ValueError("migration_arm_metrics_invalid")
    if (
        not isinstance(arm["hard_gates"], dict)
        or set(arm["hard_gates"]) != {"passed"}
        or type(arm["hard_gates"]["passed"]) is not bool
    ):
        raise ValueError("migration_arm_gates_invalid")
    return deepcopy(arm)


def _validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "min_pass_rate",
        "min_improvement",
        "max_p95_regression_ratio",
        "max_training_cost",
    }
    numeric = required - {"version"}
    if (
        not isinstance(policy, dict)
        or set(policy) != required
        or not isinstance(policy["version"], str)
        or not policy["version"]
        or any(type(policy[key]) not in {int, float} or policy[key] < 0 for key in numeric)
        or policy["min_pass_rate"] > 1
        or policy["min_improvement"] > 1
    ):
        raise ValueError("migration_policy_invalid")
    return deepcopy(policy)


def _validate_learning_source(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict) or source.get("kind") not in {
        "compile_decision",
        "compile_manifest",
    }:
        raise ValueError("migration_learning_source_invalid")
    expected = {"kind", "ref", "sha256", "value"}
    if source["kind"] == "compile_decision":
        expected.add("reason")
    if set(source) != expected or not isinstance(source["ref"], str) or not source["ref"]:
        raise ValueError("migration_learning_source_invalid")
    _sha(source["sha256"], "migration_learning_source_hash_invalid")
    value = (
        validate_compile_decision(source.get("value"))
        if source["kind"] == "compile_decision"
        else validate_compile_manifest(source.get("value"))
    )
    if sha256(canonical_bytes(value)) != source["sha256"]:
        raise ValueError("migration_learning_source_hash_invalid")
    if source["kind"] == "compile_decision" and source["reason"] != value["reason"]:
        raise ValueError("migration_learning_source_invalid")
    return deepcopy(source)


def _decision(
    arms: list[dict[str, Any]], learning_source: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    base = next(item for item in arms if item["name"] == "base")
    candidate = next((item for item in arms if item["name"] == "gap_sft"), None)
    if learning_source["kind"] == "compile_decision":
        if learning_source["reason"] == "target_release_policy_passed":
            if (
                base["hard_gates"]["passed"]
                and base["metrics"]["pass_rate"] >= policy["min_pass_rate"]
            ):
                return {
                    "status": "NO-TRAIN",
                    "reason": "base_policy_passed",
                    "selected_arm": "base",
                }
            return {
                "status": "BLOCKED",
                "reason": "base_policy_evidence_mismatch",
                "selected_arm": None,
            }
        return {"status": "BLOCKED", "reason": "candidate_unavailable", "selected_arm": None}
    if candidate is None:
        return {"status": "BLOCKED", "reason": "candidate_evaluation_missing", "selected_arm": None}
    if candidate["invalid_trials"] or candidate["valid_trials"] != candidate["required_trials"]:
        return {"status": "BLOCKED", "reason": "candidate_trials_invalid", "selected_arm": None}
    if candidate["metrics"]["training_cost"] is None:
        return {"status": "BLOCKED", "reason": "training_cost_missing", "selected_arm": None}
    if base["hard_gates"]["passed"] and base["metrics"]["pass_rate"] >= policy["min_pass_rate"]:
        return {"status": "NO-TRAIN", "reason": "base_policy_passed", "selected_arm": "base"}
    improvement = candidate["metrics"]["pass_rate"] - base["metrics"]["pass_rate"]
    latency_limit = base["metrics"]["p95_latency_ms"] * policy["max_p95_regression_ratio"]
    if (
        not candidate["hard_gates"]["passed"]
        or improvement < policy["min_improvement"]
        or candidate["metrics"]["p95_latency_ms"] > latency_limit
        or candidate["metrics"]["training_cost"] > policy["max_training_cost"]
    ):
        return {"status": "NO-GO", "reason": "candidate_policy_failed", "selected_arm": None}
    if candidate["metrics"]["pass_rate"] >= policy["min_pass_rate"]:
        return {"status": "GO", "reason": "gap_sft_policy_passed", "selected_arm": "gap_sft"}
    return {"status": "NO-GO", "reason": "candidate_capability_insufficient", "selected_arm": None}


def build_migration_report(
    *,
    tenant_id: str,
    target_fingerprint: dict[str, Any],
    learning_source: dict[str, Any],
    arms: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    target = validate_model_fingerprint(target_fingerprint)
    source = _validate_learning_source(learning_source)
    normalized_policy = _validate_policy(policy)
    normalized_arms = [_validate_arm(item) for item in arms]
    if not normalized_arms or [item["name"] for item in normalized_arms].count("base") != 1:
        raise ValueError("migration_base_arm_missing")
    if len({item["name"] for item in normalized_arms}) != len(normalized_arms):
        raise ValueError("migration_arm_duplicate")
    base = next(item for item in normalized_arms if item["name"] == "base")
    alignment_keys = (
        "task_bundle_ids",
        "environment_initial_state_sha256",
        "generation_policy_sha256",
        "verifier_contract_digest",
        "required_trials",
    )
    if any(
        any(item[key] != base[key] for key in alignment_keys)
        for item in normalized_arms
        if item["name"] != "base"
    ):
        raise ValueError("migration_arm_alignment_mismatch")
    report = {
        "schema_version": "model_migration_report.v1",
        "tenant_id": tenant_id,
        "target": {
            "fingerprint": target,
            "fingerprint_sha256": model_fingerprint_digest(target),
        },
        "learning_source": source,
        "alignment": {key: deepcopy(base[key]) for key in alignment_keys},
        "arms": normalized_arms,
        "policy": normalized_policy,
        "decision": _decision(normalized_arms, source, normalized_policy),
    }
    return validate_migration_report(report)


def validate_migration_report(  # noqa: C901 - fail-closed schema
    report: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "schema_version",
        "tenant_id",
        "target",
        "learning_source",
        "alignment",
        "arms",
        "policy",
        "decision",
    }
    if (
        not isinstance(report, dict)
        or set(report) != required
        or report["schema_version"] != "model_migration_report.v1"
        or not isinstance(report["tenant_id"], str)
        or not report["tenant_id"]
    ):
        raise ValueError("migration_report_invalid")
    if not isinstance(report["target"], dict):
        raise ValueError("migration_target_invalid")
    target = validate_model_fingerprint(report["target"].get("fingerprint"))
    if report["target"].get("fingerprint_sha256") != model_fingerprint_digest(target):
        raise ValueError("migration_target_invalid")
    source = _validate_learning_source(report["learning_source"])
    source_value = source["value"]
    if source_value["target"]["fingerprint_sha256"] != report["target"]["fingerprint_sha256"]:
        raise ValueError("migration_learning_target_mismatch")
    policy = _validate_policy(report["policy"])
    if not isinstance(report["arms"], list):
        raise ValueError("migration_base_arm_missing")
    arms = [_validate_arm(item) for item in report["arms"]]
    if [item["name"] for item in arms].count("base") != 1:
        raise ValueError("migration_base_arm_missing")
    if len({item["name"] for item in arms}) != len(arms):
        raise ValueError("migration_arm_duplicate")
    expected = _decision(arms, source, policy)
    if report["decision"] != expected:
        raise ValueError("migration_decision_mismatch")
    base = next((item for item in arms if item["name"] == "base"), None)
    if base is None or base["fingerprint_sha256"] != report["target"]["fingerprint_sha256"]:
        raise ValueError("migration_base_arm_invalid")
    alignment_keys = (
        "task_bundle_ids",
        "environment_initial_state_sha256",
        "generation_policy_sha256",
        "verifier_contract_digest",
        "required_trials",
    )
    if any(
        any(item[key] != base[key] for key in alignment_keys)
        for item in arms
        if item["name"] != "base"
    ):
        raise ValueError("migration_arm_alignment_mismatch")
    if base is None or report["alignment"] != {key: base[key] for key in alignment_keys}:
        raise ValueError("migration_alignment_invalid")
    return deepcopy(report)


def publish_migration_report(store: EvidenceObjectStore, report: dict[str, Any]) -> dict[str, str]:
    report = validate_migration_report(report)
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{report['tenant_id']}/migration/reports/sha256/{digest}.json"
    _put_immutable(store, ref, body)
    return {"report_ref": ref, "report_sha256": digest}


def _dpo_gate_result(migration_decision: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if migration_decision.get("status") == "GO":
        gates = {
            "sft_validated": {"status": "passed", "reason": None},
            "quality_gap_verified": {"status": "not_evaluated", "reason": "evidence_required"},
            "comparable_pairs_verified": {
                "status": "not_evaluated",
                "reason": "quality_gap_unverified",
            },
            "preference_calibrated": {
                "status": "not_evaluated",
                "reason": "quality_gap_unverified",
            },
            "preference_training_allowed": {
                "status": "not_evaluated",
                "reason": "quality_gap_unverified",
            },
        }
        return gates, {"status": "NOT-ENABLED", "reason": "quality_gap_unverified"}
    gates = {
        "sft_validated": {
            "status": "failed",
            "reason": f"migration_{str(migration_decision.get('status')).lower()}",
        },
        "quality_gap_verified": {"status": "not_evaluated", "reason": "sft_not_validated"},
        "comparable_pairs_verified": {
            "status": "not_evaluated",
            "reason": "sft_not_validated",
        },
        "preference_calibrated": {
            "status": "not_evaluated",
            "reason": "sft_not_validated",
        },
        "preference_training_allowed": {
            "status": "not_evaluated",
            "reason": "sft_not_validated",
        },
    }
    return gates, {"status": "NOT-ENABLED", "reason": "sft_not_validated"}


def build_dpo_gate_decision(
    *,
    tenant_id: str,
    migration_report: dict[str, Any],
    migration_report_ref: str,
    migration_report_sha256: str,
) -> dict[str, Any]:
    """Decide whether evidence permits implementing DPO; never invent missing inputs."""
    migration = validate_migration_report(migration_report)
    if migration["tenant_id"] != tenant_id:
        raise ValueError("dpo_gate_tenant_mismatch")
    if sha256(canonical_bytes(migration)) != migration_report_sha256:
        raise ValueError("dpo_gate_migration_hash_mismatch")
    gates, decision = _dpo_gate_result(migration["decision"])
    return validate_dpo_gate_decision(
        {
            "schema_version": "dpo_gate_decision.v1",
            "tenant_id": tenant_id,
            "migration_report": {
                "ref": migration_report_ref,
                "sha256": migration_report_sha256,
                "decision": migration["decision"],
            },
            "target_fingerprint_sha256": migration["target"]["fingerprint_sha256"],
            "gates": gates,
            "decision": decision,
        }
    )


def validate_dpo_gate_decision(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "tenant_id",
        "migration_report",
        "target_fingerprint_sha256",
        "gates",
        "decision",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema_version"] != "dpo_gate_decision.v1"
        or not isinstance(value["tenant_id"], str)
        or not value["tenant_id"]
    ):
        raise ValueError("dpo_gate_invalid")
    source = value["migration_report"]
    if (
        not isinstance(source, dict)
        or set(source) != {"ref", "sha256", "decision"}
        or not isinstance(source["ref"], str)
        or not source["ref"]
    ):
        raise ValueError("dpo_gate_migration_invalid")
    _sha(source["sha256"], "dpo_gate_migration_hash_invalid")
    _sha(value["target_fingerprint_sha256"], "dpo_gate_target_invalid")
    gates, decision = _dpo_gate_result(source["decision"])
    if value["gates"] != gates or value["decision"] != decision:
        raise ValueError("dpo_gate_decision_mismatch")
    return deepcopy(value)


def publish_dpo_gate_decision(
    store: EvidenceObjectStore, decision: dict[str, Any]
) -> dict[str, str]:
    decision = validate_dpo_gate_decision(decision)
    body = canonical_bytes(decision)
    digest = sha256(body)
    ref = f"tenants/{decision['tenant_id']}/learning/gates/dpo/sha256/{digest}.json"
    _put_immutable(store, ref, body)
    return {"decision_ref": ref, "decision_sha256": digest}


def _rl_gate_result(
    dpo_decision: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if dpo_decision.get("status") == "ENABLED":
        upstream = {
            "status": "not_evaluated",
            "reason": "sft_dpo_outcome_evidence_required",
        }
        decision_reason = "upstream_learning_outcome_unverified"
    else:
        upstream = {"status": "failed", "reason": "dpo_not_enabled"}
        decision_reason = "upstream_learning_gates_not_satisfied"
    deferred_reason = (
        "upstream_learning_not_exhausted"
        if upstream["status"] == "failed"
        else "upstream_learning_outcome_unverified"
    )
    gates = {
        "upstream_learning_exhausted": upstream,
        "environment_batch_reset": {
            "status": "not_evaluated",
            "reason": deferred_reason,
        },
        "reward_calibrated": {"status": "not_evaluated", "reason": deferred_reason},
        "reward_hacking_resistant": {
            "status": "not_evaluated",
            "reason": deferred_reason,
        },
        "token_telemetry_verified": {
            "status": "not_evaluated",
            "reason": deferred_reason,
        },
        "training_budget_approved": {
            "status": "not_evaluated",
            "reason": deferred_reason,
        },
    }
    return (
        gates,
        {"status": "NOT-ENABLED", "reason": decision_reason},
        {"status": "NOT-SELECTED", "reason": "rl_not_enabled"},
    )


def build_rl_gate_decision(
    *,
    tenant_id: str,
    dpo_gate_decision: dict[str, Any],
    dpo_gate_decision_ref: str,
    dpo_gate_decision_sha256: str,
) -> dict[str, Any]:
    """Decide whether evidence permits an RL PoC and selecting its execution backend."""
    dpo = validate_dpo_gate_decision(dpo_gate_decision)
    if dpo["tenant_id"] != tenant_id:
        raise ValueError("rl_gate_tenant_mismatch")
    if sha256(canonical_bytes(dpo)) != dpo_gate_decision_sha256:
        raise ValueError("rl_gate_dpo_hash_mismatch")
    gates, decision, agent_lightning = _rl_gate_result(dpo["decision"])
    return validate_rl_gate_decision(
        {
            "schema_version": "rl_gate_decision.v1",
            "tenant_id": tenant_id,
            "dpo_gate_decision": {
                "ref": dpo_gate_decision_ref,
                "sha256": dpo_gate_decision_sha256,
                "decision": dpo["decision"],
            },
            "target_fingerprint_sha256": dpo["target_fingerprint_sha256"],
            "gates": gates,
            "decision": decision,
            "agent_lightning": agent_lightning,
        }
    )


def validate_rl_gate_decision(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "tenant_id",
        "dpo_gate_decision",
        "target_fingerprint_sha256",
        "gates",
        "decision",
        "agent_lightning",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema_version"] != "rl_gate_decision.v1"
        or not isinstance(value["tenant_id"], str)
        or not value["tenant_id"]
    ):
        raise ValueError("rl_gate_invalid")
    source = value["dpo_gate_decision"]
    if (
        not isinstance(source, dict)
        or set(source) != {"ref", "sha256", "decision"}
        or not isinstance(source["ref"], str)
        or not source["ref"]
    ):
        raise ValueError("rl_gate_dpo_invalid")
    _sha(source["sha256"], "rl_gate_dpo_hash_invalid")
    _sha(value["target_fingerprint_sha256"], "rl_gate_target_invalid")
    gates, decision, agent_lightning = _rl_gate_result(source["decision"])
    if (
        value["gates"] != gates
        or value["decision"] != decision
        or value["agent_lightning"] != agent_lightning
    ):
        raise ValueError("rl_gate_decision_mismatch")
    return deepcopy(value)


def publish_rl_gate_decision(
    store: EvidenceObjectStore, decision: dict[str, Any]
) -> dict[str, str]:
    decision = validate_rl_gate_decision(decision)
    body = canonical_bytes(decision)
    digest = sha256(body)
    ref = f"tenants/{decision['tenant_id']}/learning/gates/rl/sha256/{digest}.json"
    _put_immutable(store, ref, body)
    return {"decision_ref": ref, "decision_sha256": digest}
