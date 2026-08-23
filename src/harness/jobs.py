"""Preflight contracts for H5 training and evaluation workers."""

from __future__ import annotations

from typing import Any


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
    if context["harness_version"] != 5:
        raise ValueError("h5_training_context_version_invalid")
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
    return dict(context)
