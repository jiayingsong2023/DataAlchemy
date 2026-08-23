"""Canonical contracts for reusable agent-learning tasks and execution evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.core.evidence import EvidenceObjectStore, ObjectNotFound, canonical_bytes, sha256

if TYPE_CHECKING:
    from src.core.verifiers import VerificationResult, VerifierSpec

_HEX = frozenset("0123456789abcdef")
_FORBIDDEN_KEY_PARTS = (
    "access_token",
    "api_key",
    "cookie",
    "credential",
    "password",
    "secret",
)
_HIDDEN_KEYS = frozenset(
    {
        "expected_answer",
        "gold_answer",
        "hidden_assertion",
        "hidden_assertions",
        "reference_answer",
        "verifier_input",
    }
)
_VERIFIER_TO_TRIAL = {"passed": "succeeded", "failed": "failed", "blocked": "invalidated"}


def _object(value: Any, error: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(error)
    return value


def _exact_keys(value: dict[str, Any], required: set[str], error: str) -> None:
    if set(value) != required:
        raise ValueError(error)


def _text(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(error)
    return value


def _positive_int(value: Any, error: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(error)
    return value


def _sha256(value: Any, error: str, *, prefixed: bool = False) -> str:
    value = _text(value, error)
    digest = value.removeprefix("sha256:") if prefixed else value
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise ValueError(error)
    if prefixed and not value.startswith("sha256:"):
        raise ValueError(error)
    return value


def _scan_forbidden(
    value: Any, tenant_id: str | None = None, *, allow_hidden: bool = False
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if not allow_hidden and normalized in _HIDDEN_KEYS:
                raise ValueError("task_bundle_hidden_answer_forbidden")
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError("contract_secret_field_forbidden")
            if normalized.endswith("tenant_id") and tenant_id is not None and child != tenant_id:
                raise ValueError("contract_tenant_mismatch")
            _scan_forbidden(child, tenant_id, allow_hidden=allow_hidden)
    elif isinstance(value, list):
        for child in value:
            _scan_forbidden(child, tenant_id, allow_hidden=allow_hidden)


def _contract_list(value: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"task_bundle_{kind}_missing")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in value:
        item = _object(item, f"task_bundle_{kind}_invalid")
        _exact_keys(item, {"name", "version", "contract_sha256"}, f"task_bundle_{kind}_invalid")
        name = _text(item["name"], f"task_bundle_{kind}_name_invalid")
        version = _positive_int(item["version"], f"task_bundle_{kind}_version_invalid")
        _sha256(item["contract_sha256"], f"task_bundle_{kind}_hash_invalid")
        if (name, version) in seen:
            raise ValueError(f"task_bundle_{kind}_duplicate")
        seen.add((name, version))
        normalized.append(dict(item))
    return normalized


def validate_task_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a detached canonical ``task_bundle.v1`` value."""
    bundle = _object(bundle, "task_bundle_invalid")
    _scan_forbidden(bundle)
    _exact_keys(
        bundle,
        {"schema_version", "task", "environment", "tools", "verifiers", "limits", "governance"},
        "task_bundle_fields_invalid",
    )
    if bundle["schema_version"] != "task_bundle.v1":
        raise ValueError("task_bundle_schema_invalid")

    task = _object(bundle["task"], "task_bundle_task_invalid")
    _exact_keys(
        task,
        {"case_id", "type", "input_ref", "input_sha256", "input_tenant_id", "split"},
        "task_bundle_task_fields_invalid",
    )
    _text(task["case_id"], "task_bundle_case_id_invalid")
    _text(task["type"], "task_bundle_type_invalid")
    _text(task["input_ref"], "task_bundle_input_ref_invalid")
    _sha256(task["input_sha256"], "task_bundle_input_hash_invalid")
    _text(task["input_tenant_id"], "task_bundle_input_tenant_invalid")
    if task["split"] not in {"train", "validation", "evaluation", "evaluation_holdout"}:
        raise ValueError("task_bundle_split_invalid")

    environment = _object(bundle["environment"], "task_bundle_environment_invalid")
    _exact_keys(
        environment,
        {"snapshot_ref", "snapshot_sha256", "snapshot_tenant_id", "reset_contract"},
        "task_bundle_environment_fields_invalid",
    )
    _text(environment["snapshot_ref"], "task_bundle_environment_ref_invalid")
    _sha256(environment["snapshot_sha256"], "task_bundle_environment_hash_invalid")
    _text(environment["snapshot_tenant_id"], "task_bundle_environment_tenant_invalid")
    reset = _object(environment["reset_contract"], "task_bundle_reset_invalid")
    _exact_keys(reset, {"kind", "ref", "sha256"}, "task_bundle_reset_fields_invalid")
    if reset["kind"] != "registered-script":
        raise ValueError("task_bundle_reset_kind_invalid")
    _text(reset["ref"], "task_bundle_reset_ref_invalid")
    _sha256(reset["sha256"], "task_bundle_reset_hash_invalid")

    _contract_list(bundle["tools"], "tools")
    _contract_list(bundle["verifiers"], "verifiers")

    limits = _object(bundle["limits"], "task_bundle_limits_invalid")
    _exact_keys(limits, {"max_steps", "deadline_seconds"}, "task_bundle_limits_fields_invalid")
    _positive_int(limits["max_steps"], "task_bundle_max_steps_invalid")
    _positive_int(limits["deadline_seconds"], "task_bundle_deadline_invalid")

    governance = _object(bundle["governance"], "task_bundle_governance_invalid")
    _exact_keys(
        governance,
        {"tenant_id", "acl_sha256", "permission_version", "retention_until"},
        "task_bundle_governance_fields_invalid",
    )
    tenant_id = _text(governance["tenant_id"], "task_bundle_tenant_invalid")
    _sha256(governance["acl_sha256"], "task_bundle_acl_hash_invalid")
    _text(governance["permission_version"], "task_bundle_permission_invalid")
    retention = _text(governance["retention_until"], "task_bundle_retention_invalid")
    try:
        parsed_retention = datetime.fromisoformat(retention.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("task_bundle_retention_invalid") from error
    if parsed_retention.tzinfo is None:
        raise ValueError("task_bundle_retention_timezone_missing")

    _scan_forbidden(bundle, tenant_id)
    return deepcopy(bundle)


def task_bundle_id(bundle: dict[str, Any]) -> str:
    """Return the stable content address of a valid Task Bundle."""
    return "sha256:" + sha256(canonical_bytes(validate_task_bundle(bundle)))


def validate_task_bundle_fingerprint(fingerprint: dict[str, Any]) -> dict[str, Any]:
    """Require the Task Bundle and its private/public inputs on every trial."""
    fingerprint = _object(fingerprint, "trial_fingerprint_invalid")
    required = {
        "task_bundle_id",
        "task_bundle_ref",
        "task_bundle_sha256",
        "task_input_ref",
        "task_input_sha256",
        "verifier_input_ref",
        "verifier_input_sha256",
    }
    if not required <= fingerprint.keys():
        raise ValueError("trial_task_bundle_fingerprint_missing")
    bundle_id = _sha256(
        fingerprint["task_bundle_id"], "trial_task_bundle_id_invalid", prefixed=True
    )
    bundle_sha256 = _sha256(
        fingerprint["task_bundle_sha256"], "trial_task_bundle_hash_invalid"
    )
    if bundle_id != "sha256:" + bundle_sha256:
        raise ValueError("trial_task_bundle_id_mismatch")
    for name in ("task_bundle_ref", "task_input_ref", "verifier_input_ref"):
        _text(fingerprint[name], f"trial_{name}_invalid")
    _sha256(fingerprint["task_input_sha256"], "trial_task_input_hash_invalid")
    _sha256(fingerprint["verifier_input_sha256"], "trial_verifier_input_hash_invalid")
    return deepcopy(fingerprint)


def _put_immutable(store: EvidenceObjectStore, key: str, body: bytes) -> None:
    try:
        existing = store.get(key)
    except ObjectNotFound:
        store.put(key, body)
    else:
        if existing != body:
            raise RuntimeError("task_asset_key_conflict")
    if store.get(key) != body:
        raise RuntimeError("task_asset_publish_mismatch")


def publish_rag_task_bundle(
    store: EvidenceObjectStore,
    case: dict[str, Any],
    *,
    tenant_id: str,
    environment_snapshot: dict[str, Any],
    reset_contract: dict[str, Any],
    tool_contract: dict[str, Any],
    verifier_name: str,
    verifier_version: int,
    limits: dict[str, Any],
    acl_sha256: str,
    permission_version: str,
    retention_until: str,
) -> dict[str, Any]:
    """Publish one sanitized PDF/RAG Task Bundle and its verifier-only criteria."""
    case = _object(case, "task_case_invalid")
    _scan_forbidden(case, allow_hidden=True)
    environment_snapshot = _object(environment_snapshot, "task_environment_snapshot_invalid")
    _scan_forbidden(environment_snapshot)
    case_id = _text(case.get("case_id"), "task_case_id_invalid")
    query = _text(case.get("query"), "task_case_query_invalid")
    tenant_id = _text(tenant_id, "task_bundle_tenant_invalid")
    model_input = {"case_id": case_id, "query": query}
    verifier_input = {
        "schema_version": "rag_verifier_input.v1",
        "case_id": case_id,
        "criteria": {
            key: value
            for key, value in case.items()
            if key not in {"case_id", "input_sha256", "query"}
        },
    }
    if not verifier_input["criteria"]:
        raise ValueError("task_case_verifier_criteria_missing")

    input_body = canonical_bytes(model_input)
    input_sha256 = sha256(input_body)
    environment_body = canonical_bytes(environment_snapshot)
    environment_sha256 = sha256(environment_body)
    verifier_body = canonical_bytes(verifier_input)
    verifier_sha256 = sha256(verifier_body)
    base = f"tenants/{tenant_id}/task-assets"
    input_ref = f"{base}/inputs/sha256/{input_sha256}.json"
    environment_ref = f"{base}/environments/sha256/{environment_sha256}.json"
    verifier_ref = f"{base}/verifier-inputs/sha256/{verifier_sha256}.json"

    bundle = validate_task_bundle(
        {
            "schema_version": "task_bundle.v1",
            "task": {
                "case_id": case_id,
                "type": "rag_answer_with_citation",
                "input_ref": input_ref,
                "input_sha256": input_sha256,
                "input_tenant_id": tenant_id,
                "split": "evaluation_holdout",
            },
            "environment": {
                "snapshot_ref": environment_ref,
                "snapshot_sha256": environment_sha256,
                "snapshot_tenant_id": tenant_id,
                "reset_contract": reset_contract,
            },
            "tools": [tool_contract],
            "verifiers": [
                {
                    "name": verifier_name,
                    "version": verifier_version,
                    "contract_sha256": verifier_sha256,
                }
            ],
            "limits": limits,
            "governance": {
                "tenant_id": tenant_id,
                "acl_sha256": acl_sha256,
                "permission_version": permission_version,
                "retention_until": retention_until,
            },
        }
    )
    bundle_body = canonical_bytes(bundle)
    bundle_sha256 = sha256(bundle_body)
    bundle_ref = f"{base}/bundles/sha256/{bundle_sha256}.json"
    for key, body in (
        (input_ref, input_body),
        (environment_ref, environment_body),
        (verifier_ref, verifier_body),
        (bundle_ref, bundle_body),
    ):
        _put_immutable(store, key, body)
    return {
        "fingerprint": {
            "task_bundle_id": f"sha256:{bundle_sha256}",
            "task_bundle_ref": bundle_ref,
            "task_bundle_sha256": bundle_sha256,
            "task_input_ref": input_ref,
            "task_input_sha256": input_sha256,
            "verifier_input_ref": verifier_ref,
            "verifier_input_sha256": verifier_sha256,
        },
        "model_input": model_input,
        "verifier_input": verifier_input,
    }


def _evidence_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("evidence_refs_invalid")
    for item in value:
        item = _object(item, "evidence_ref_invalid")
        _exact_keys(item, {"ref", "sha256"}, "evidence_ref_fields_invalid")
        _text(item["ref"], "evidence_ref_missing")
        _sha256(item["sha256"], "evidence_ref_hash_invalid")
    return value


def _validate_reset(value: Any) -> dict[str, Any]:
    reset = _object(value, "environment_reset_invalid")
    _exact_keys(
        reset,
        {"receipt_id", "plan_sha256", "status", "error_code"},
        "environment_reset_fields_invalid",
    )
    _text(reset["receipt_id"], "environment_reset_receipt_id_invalid")
    _sha256(reset["plan_sha256"], "environment_reset_plan_hash_invalid")
    if reset["status"] not in {"reset_complete", "reset_failed"}:
        raise ValueError("environment_reset_status_invalid")
    if reset["status"] == "reset_failed" and not reset["error_code"]:
        raise ValueError("environment_reset_error_missing")
    if reset["status"] == "reset_complete" and reset["error_code"] is not None:
        raise ValueError("environment_reset_error_unexpected")
    return reset


def _validate_runtime(value: Any) -> None:
    runtime = _object(value, "environment_runtime_invalid")
    _exact_keys(
        runtime,
        {"image_digest", "tool_contracts_sha256"},
        "environment_runtime_fields_invalid",
    )
    _sha256(runtime["image_digest"], "environment_image_digest_invalid", prefixed=True)
    _sha256(runtime["tool_contracts_sha256"], "environment_tools_hash_invalid")


def _validate_preflight(value: Any) -> dict[str, Any]:
    preflight = _object(value, "environment_preflight_invalid")
    _exact_keys(
        preflight,
        {"status", "error_code", "evidence_refs"},
        "environment_preflight_fields_invalid",
    )
    if preflight["status"] not in {"passed", "failed"}:
        raise ValueError("environment_preflight_status_invalid")
    if preflight["status"] == "failed" and not preflight["error_code"]:
        raise ValueError("environment_preflight_error_missing")
    if preflight["status"] == "passed" and preflight["error_code"] is not None:
        raise ValueError("environment_preflight_error_unexpected")
    _evidence_refs(preflight["evidence_refs"])
    return preflight


def _validate_cleanup(value: Any) -> dict[str, Any]:
    cleanup = _object(value, "environment_cleanup_invalid")
    _exact_keys(cleanup, {"status", "error_code"}, "environment_cleanup_fields_invalid")
    if cleanup["status"] not in {"not_started", "completed", "failed"}:
        raise ValueError("environment_cleanup_status_invalid")
    if cleanup["status"] == "failed" and not cleanup["error_code"]:
        raise ValueError("environment_cleanup_error_missing")
    if cleanup["status"] != "failed" and cleanup["error_code"] is not None:
        raise ValueError("environment_cleanup_error_unexpected")
    return cleanup


def validate_environment_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable reset/preflight evidence used by a rollout."""
    receipt = _object(receipt, "environment_receipt_invalid")
    _exact_keys(
        receipt,
        {
            "schema_version",
            "task_bundle_id",
            "environment_id",
            "registry_sha256",
            "reset",
            "fixture_sha256",
            "runtime",
            "preflight",
            "initial_state_sha256",
            "final_state_delta_sha256",
            "cleanup",
            "state",
            "invalid_reason",
        },
        "environment_receipt_fields_invalid",
    )
    if receipt["schema_version"] != "environment_receipt.v1":
        raise ValueError("environment_receipt_schema_invalid")
    _sha256(receipt["task_bundle_id"], "environment_task_bundle_id_invalid", prefixed=True)
    _text(receipt["environment_id"], "environment_id_invalid")
    _sha256(receipt["registry_sha256"], "environment_registry_hash_invalid")
    _sha256(receipt["fixture_sha256"], "environment_fixture_hash_invalid")

    reset = _validate_reset(receipt["reset"])
    _validate_runtime(receipt["runtime"])
    preflight = _validate_preflight(receipt["preflight"])
    cleanup = _validate_cleanup(receipt["cleanup"])

    state = receipt["state"]
    if state not in {"ready", "invalidated"}:
        raise ValueError("environment_receipt_state_invalid")
    if state == "ready":
        if reset["status"] != "reset_complete" or preflight["status"] != "passed":
            raise ValueError("environment_ready_without_preflight")
        _sha256(receipt["initial_state_sha256"], "environment_initial_state_hash_invalid")
        if receipt["invalid_reason"] is not None:
            raise ValueError("environment_ready_invalid_reason_present")
    else:
        _text(receipt["invalid_reason"], "environment_invalid_reason_missing")
        if (
            reset["status"] == "reset_complete"
            and preflight["status"] == "passed"
            and cleanup["status"] != "failed"
        ):
            raise ValueError("environment_invalid_without_failure")
        if receipt["initial_state_sha256"] is not None:
            _sha256(receipt["initial_state_sha256"], "environment_initial_state_hash_invalid")

    if receipt["final_state_delta_sha256"] is not None:
        _sha256(receipt["final_state_delta_sha256"], "environment_final_delta_hash_invalid")

    _scan_forbidden(receipt)
    return deepcopy(receipt)


def publish_environment_receipt(
    store: EvidenceObjectStore,
    receipt: dict[str, Any],
    preflight_evidence: dict[str, Any],
    *,
    tenant_id: str,
) -> dict[str, str]:
    """Publish one validated receipt and its preflight evidence immutably."""
    receipt = validate_environment_receipt(receipt)
    _scan_forbidden(preflight_evidence, tenant_id)
    preflight_body = canonical_bytes(preflight_evidence)
    preflight_sha256 = sha256(preflight_body)
    expected_ref = (
        f"tenants/{tenant_id}/environment-evidence/preflight/{preflight_sha256}.json"
    )
    if receipt["preflight"]["evidence_refs"] != [
        {"ref": expected_ref, "sha256": preflight_sha256}
    ]:
        raise ValueError("environment_preflight_evidence_mismatch")
    receipt_body = canonical_bytes(receipt)
    receipt_sha256 = sha256(receipt_body)
    receipt_ref = (
        f"tenants/{tenant_id}/environment-evidence/receipts/{receipt_sha256}.json"
    )
    _put_immutable(store, expected_ref, preflight_body)
    _put_immutable(store, receipt_ref, receipt_body)
    return {"receipt_ref": receipt_ref, "receipt_sha256": receipt_sha256}


def validate_verifier_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the versioned projection of one existing verifier result."""
    payload = _object(payload, "verifier_result_invalid")
    _exact_keys(
        payload,
        {
            "schema_version",
            "verifier",
            "raw_status",
            "trial_state",
            "hard_gates",
            "scores",
            "failure_code",
            "evidence_refs",
            "summary",
        },
        "verifier_result_fields_invalid",
    )
    if payload["schema_version"] != "verifier_result.v1":
        raise ValueError("verifier_result_schema_invalid")
    verifier = _object(payload["verifier"], "verifier_identity_invalid")
    _exact_keys(
        verifier,
        {"name", "version", "contract_digest"},
        "verifier_identity_fields_invalid",
    )
    _text(verifier["name"], "verifier_name_invalid")
    _positive_int(verifier["version"], "verifier_version_invalid")
    _sha256(verifier["contract_digest"], "verifier_contract_digest_invalid")

    raw_status = payload["raw_status"]
    if raw_status not in _VERIFIER_TO_TRIAL:
        raise ValueError("verifier_status_invalid")
    if payload["trial_state"] != _VERIFIER_TO_TRIAL[raw_status]:
        raise ValueError("verifier_trial_state_mismatch")
    failure_code = payload["failure_code"]
    if raw_status == "passed" and failure_code is not None:
        raise ValueError("verifier_passed_failure_code_present")
    if raw_status != "passed":
        _text(failure_code, "verifier_failure_code_missing")

    hard_gates = _object(payload["hard_gates"], "verifier_hard_gates_invalid")
    if not hard_gates or any(type(value) is not bool for value in hard_gates.values()):
        raise ValueError("verifier_hard_gates_invalid")
    scores = _object(payload["scores"], "verifier_scores_invalid")
    if any(type(value) not in {int, float} for value in scores.values()):
        raise ValueError("verifier_scores_invalid")
    _evidence_refs(payload["evidence_refs"])
    _object(payload["summary"], "verifier_summary_invalid")
    _scan_forbidden(payload)
    return deepcopy(payload)


def project_verification_result(
    spec: VerifierSpec,
    result: VerificationResult,
    *,
    hard_gates: dict[str, bool] | None = None,
    scores: dict[str, int | float] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Project the current verifier types into the frozen learning contract."""
    payload = {
        "schema_version": "verifier_result.v1",
        "verifier": {
            "name": spec.name,
            "version": spec.version,
            "contract_digest": spec.contract_digest,
        },
        "raw_status": result.status,
        "trial_state": _VERIFIER_TO_TRIAL.get(result.status),
        "hard_gates": hard_gates or {"passed": result.status == "passed"},
        "scores": scores or {},
        "failure_code": result.error_code,
        "evidence_refs": evidence_refs or [],
        "summary": result.summary,
    }
    return validate_verifier_result(payload)
