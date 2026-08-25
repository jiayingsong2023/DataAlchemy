"""Preflight contracts for H5 training and evaluation workers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from harness.evaluation import validate_model_fingerprint


def validate_gap_base_evaluation(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Accept a complete, valid base measurement even when capability is below policy."""
    gates = evaluation.get("hard_gates", {})
    metrics = evaluation.get("metrics", {})
    if (
        evaluation.get("subject_type") != "base"
        or evaluation.get("state") not in {"passed", "failed"}
        or gates.get("independent_verifier") is not True
        or gates.get("invalidated_trials") != 0
        or gates.get("judge_only") is not False
        or metrics.get("total") != evaluation.get("required_trials")
    ):
        raise ValueError("h6_base_evaluation_unusable")
    return dict(evaluation)


def validate_training_context(context: dict[str, Any]) -> dict[str, Any]:
    required = {
        "harness_version",
        "run_id",
        "tenant_id",
        "username",
        "role",
        "snapshot_id",
        "snapshot_state",
        "dataset_key",
        "dataset_sha256",
        "base_model_digest",
        "tokenizer_digest",
        "model_id",
        "database_url",
        "base_evaluation_id",
        "base_evaluation_passed",
        "output_prefix",
    }
    if not isinstance(context, dict) or not required <= context.keys():
        raise ValueError("h5_training_context_incomplete")
    if context["harness_version"] not in {5, 6, 7}:
        raise ValueError("h5_training_context_version_invalid")
    if context["harness_version"] >= 6:
        compile_required = {
            "compile_manifest_ref",
            "compile_manifest_sha256",
            "chat_template_digest",
        }
        if not compile_required <= context.keys():
            raise ValueError("h6_compile_manifest_missing")
        for key in ("compile_manifest_sha256", "chat_template_digest", "tokenizer_digest"):
            value = context.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("h6_compile_manifest_invalid")
    if context["harness_version"] == 7:
        if not isinstance(context.get("adapter_id"), str) or not context["adapter_id"]:
            raise ValueError("h7_adapter_id_missing")
        if context.get("training_cost_policy") != {
            "version": "gpu-hour@1",
            "unit": "gpu_hour",
            "seconds_per_unit": 3600,
        }:
            raise ValueError("h7_training_cost_policy_invalid")
    if context["snapshot_state"] != "approved" or context["base_evaluation_passed"] is not True:
        raise ValueError("h5_training_prerequisite_failed")
    if context["role"] not in {"admin", "reviewer"}:
        raise ValueError("h5_training_worker_role_invalid")
    if (
        not context["tenant_id"]
        or not context["username"]
        or not context["run_id"]
        or not context["dataset_key"]
        or not isinstance(context["model_id"], str)
        or not context["model_id"]
    ):
        raise ValueError("h5_training_identity_missing")
    if not isinstance(context["dataset_sha256"], str) or len(context["dataset_sha256"]) != 64:
        raise ValueError("h5_dataset_hash_invalid")
    return dict(context)


def validate_evaluation_context(context: dict[str, Any]) -> dict[str, Any]:
    required = {
        "harness_version",
        "run_id",
        "tenant_id",
        "username",
        "role",
        "evaluation_id",
        "suite_sha256",
        "database_url",
        "cases",
        "verifier_cases",
    }
    if not isinstance(context, dict) or not required <= context.keys():
        raise ValueError("h5_evaluation_context_incomplete")
    if context["harness_version"] != 5 or len(context["suite_sha256"]) != 64:
        raise ValueError("h5_evaluation_context_invalid")
    if context["role"] not in {"admin", "reviewer", "user"}:
        raise ValueError("h5_evaluation_worker_role_invalid")
    if not isinstance(context["cases"], list) or not context["cases"]:
        raise ValueError("h5_evaluation_cases_missing")
    if any(not isinstance(case, dict) or not case.get("case_id") for case in context["cases"]):
        raise ValueError("h5_evaluation_case_invalid")
    if any(set(case) != {"case_id", "query"} for case in context["cases"]):
        raise ValueError("h5_evaluation_model_input_not_sanitized")
    verifier_cases = context["verifier_cases"]
    if (
        not isinstance(verifier_cases, list)
        or any(
            not isinstance(case, dict)
            or set(case) != {"schema_version", "case_id", "criteria"}
            or case.get("schema_version") != "rag_verifier_input.v1"
            or not isinstance(case.get("criteria"), dict)
            for case in verifier_cases
        )
        or {case["case_id"] for case in verifier_cases}
        != {case["case_id"] for case in context["cases"]}
    ):
        raise ValueError("h5_evaluation_verifier_cases_invalid")
    if context.get("use_adapter") and not context.get("adapter_id"):
        raise ValueError("h5_evaluation_adapter_id_missing")
    if context.get("model_id") and context.get("simulation") is not True:
        validate_model_fingerprint(context.get("model_fingerprint"))
        generation_policy = context.get("generation_policy")
        if not isinstance(generation_policy, dict):
            raise ValueError("h5_generation_policy_invalid")
        generation_sha256 = hashlib.sha256(
            json.dumps(
                generation_policy,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if context.get("generation_policy_sha256") != generation_sha256:
            raise ValueError("h5_generation_policy_hash_mismatch")
        case_ids = {case["case_id"] for case in context["cases"]}
        for key, error in (
            ("trial_ids", "h5_trial_coverage_mismatch"),
            ("task_fingerprints", "h5_task_fingerprint_coverage_mismatch"),
            ("environment_receipts", "h5_environment_receipt_coverage_mismatch"),
        ):
            value = context.get(key)
            if not isinstance(value, dict) or set(value) != case_ids:
                raise ValueError(error)
    return dict(context)
