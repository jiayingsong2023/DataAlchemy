"""Canonical contracts for reusable agent-learning tasks and execution evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from core.evidence import EvidenceObjectStore, ObjectNotFound, canonical_bytes, sha256

try:  # pytest imports the same module through the ``src.`` package alias.
    from src.core.evidence import ObjectNotFound as PackageObjectNotFound
except ImportError:  # pragma: no cover - installed runtime has only ``core``.
    PackageObjectNotFound = ObjectNotFound

if TYPE_CHECKING:
    from core.agent_runtime import AgentRuntime
    from core.verifiers import VerificationResult, VerifierSpec

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
_EXPERIENCE_EVENT_TYPES = frozenset(
    {
        "rollout_started",
        "context_built",
        "model_call",
        "tool_call",
        "tool_observation",
        "verifier_result",
        "reward",
        "user_feedback",
        "rollout_finished",
    }
)


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
    bundle_sha256 = _sha256(fingerprint["task_bundle_sha256"], "trial_task_bundle_hash_invalid")
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
    except (ObjectNotFound, PackageObjectNotFound):
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
    split = case.get("split", "evaluation_holdout")
    if split not in {"train", "validation", "evaluation", "evaluation_holdout"}:
        raise ValueError("task_bundle_split_invalid")
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
                "split": split,
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
    expected_ref = f"tenants/{tenant_id}/environment-evidence/preflight/{preflight_sha256}.json"
    if receipt["preflight"]["evidence_refs"] != [{"ref": expected_ref, "sha256": preflight_sha256}]:
        raise ValueError("environment_preflight_evidence_mismatch")
    receipt_body = canonical_bytes(receipt)
    receipt_sha256 = sha256(receipt_body)
    receipt_ref = f"tenants/{tenant_id}/environment-evidence/receipts/{receipt_sha256}.json"
    _put_immutable(store, expected_ref, preflight_body)
    _put_immutable(store, receipt_ref, receipt_body)
    return {"receipt_ref": receipt_ref, "receipt_sha256": receipt_sha256}


def finalize_environment_receipt(
    receipt: dict[str, Any],
    final_state_delta: dict[str, Any],
    *,
    cleanup_status: str,
    cleanup_error: str | None = None,
) -> dict[str, Any]:
    """Close a ready receipt with deterministic final-delta and cleanup evidence."""
    finalized = validate_environment_receipt(receipt)
    finalized["final_state_delta_sha256"] = sha256(canonical_bytes(final_state_delta))
    finalized["cleanup"] = {"status": cleanup_status, "error_code": cleanup_error}
    if cleanup_status == "failed":
        finalized["state"] = "invalidated"
        finalized["invalid_reason"] = cleanup_error or "environment_cleanup_failed"
    return validate_environment_receipt(finalized)


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


def record_experience_event(
    store: EvidenceObjectStore,
    runtime: AgentRuntime,
    identity: dict[str, str],
    task_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    producer: str,
    call_id: str | None = None,
    retry_of: str | None = None,
    parent_call_id: str | None = None,
) -> dict[str, Any]:
    """Persist restricted event content and append only its reference to ``agent_events``."""
    if event_type not in _EXPERIENCE_EVENT_TYPES:
        raise ValueError("experience_event_type_invalid")
    _text(producer, "experience_event_producer_invalid")
    _scan_forbidden(content, identity["tenant_id"], allow_hidden=True)
    task = runtime.get_task(task_id, identity)
    body = canonical_bytes(content)
    digest = sha256(body)
    key = (
        f"tenants/{identity['tenant_id']}/experiences/runs/{task['run_id']}"
        f"/events/{event_type}/sha256/{digest}.json"
    )
    _put_immutable(store, key, body)
    projection = {
        "schema_version": "experience_event_ref.v1",
        "run_id": task["run_id"],
        "type": event_type,
        "content_ref": key,
        "sha256": digest,
        "producer": producer,
        "call_id": call_id,
        "retry_of": retry_of,
        "parent_call_id": parent_call_id,
    }
    runtime.record_event(task_id, identity, "experience_event", projection)
    return projection


def validate_experience_content(
    content: dict[str, Any], tenant_id: str
) -> dict[str, Any]:
    """Reject secret-shaped or cross-tenant fields before event content is trusted."""
    content = _object(content, "experience_event_content_invalid")
    _scan_forbidden(content, tenant_id, allow_hidden=True)
    return deepcopy(content)


def validate_experience_bundle(bundle: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """Validate one publishable, model-independent rollout projection."""
    bundle = _object(bundle, "experience_bundle_invalid")
    _scan_forbidden(bundle, allow_hidden=True)
    _exact_keys(
        bundle,
        {
            "schema_version",
            "tenant_id",
            "task_bundle_id",
            "task_bundle_ref",
            "task_bundle_sha256",
            "run_id",
            "trial_id",
            "source_manifest_ref",
            "source_manifest_sha256",
            "producer",
            "environment",
            "events",
            "outcome",
            "labels",
        },
        "experience_bundle_fields_invalid",
    )
    if bundle["schema_version"] != "experience_bundle.v1":
        raise ValueError("experience_bundle_schema_invalid")
    tenant_id = _text(bundle["tenant_id"], "experience_tenant_invalid")
    bundle_id = _sha256(
        bundle["task_bundle_id"], "experience_task_bundle_id_invalid", prefixed=True
    )
    bundle_sha256 = _sha256(bundle["task_bundle_sha256"], "experience_task_bundle_hash_invalid")
    if bundle_id != f"sha256:{bundle_sha256}":
        raise ValueError("experience_task_bundle_id_mismatch")
    for name in ("task_bundle_ref", "run_id", "trial_id", "source_manifest_ref"):
        _text(bundle[name], f"experience_{name}_invalid")
    _sha256(bundle["source_manifest_sha256"], "experience_manifest_hash_invalid")

    producer = _object(bundle["producer"], "experience_producer_invalid")
    _exact_keys(
        producer,
        {
            "model_id",
            "model_sha256",
            "tokenizer_sha256",
            "chat_template_sha256",
            "adapter_sha256",
            "policy_sha256",
        },
        "experience_producer_fields_invalid",
    )
    _text(producer["model_id"], "experience_model_id_invalid")
    for name in ("model_sha256", "tokenizer_sha256", "chat_template_sha256", "policy_sha256"):
        _sha256(producer[name], f"experience_{name}_invalid")
    if producer["adapter_sha256"] is not None:
        _sha256(producer["adapter_sha256"], "experience_adapter_hash_invalid")

    environment = _object(bundle["environment"], "experience_environment_invalid")
    _exact_keys(
        environment,
        {"receipt_ref", "receipt_sha256"},
        "experience_environment_fields_invalid",
    )
    _text(environment["receipt_ref"], "experience_environment_ref_invalid")
    _sha256(environment["receipt_sha256"], "experience_environment_hash_invalid")

    events = bundle["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("experience_events_missing")
    call_ids: set[str] = set()
    required_types = {
        "context_built",
        "model_call",
        "tool_observation",
        "verifier_result",
        "rollout_finished",
    }
    for expected_sequence, event in enumerate(events, 1):
        event = _object(event, "experience_event_invalid")
        _exact_keys(
            event,
            {
                "sequence",
                "type",
                "content_ref",
                "sha256",
                "producer",
                "call_id",
                "retry_of",
                "parent_call_id",
            },
            "experience_event_fields_invalid",
        )
        if event["sequence"] != expected_sequence:
            raise ValueError("experience_event_sequence_invalid")
        if event["type"] not in _EXPERIENCE_EVENT_TYPES:
            raise ValueError("experience_event_type_invalid")
        for name in ("content_ref", "producer"):
            _text(event[name], f"experience_event_{name}_invalid")
        _sha256(event["sha256"], "experience_event_hash_invalid")
        call_id = event["call_id"]
        retry_of = event["retry_of"]
        parent_call_id = event["parent_call_id"]
        if event["type"] == "model_call":
            _text(call_id, "experience_call_id_missing")
            if call_id in call_ids:
                raise ValueError("experience_call_id_duplicate")
            if retry_of is not None and retry_of not in call_ids:
                raise ValueError("experience_retry_lineage_invalid")
            call_ids.add(call_id)
        elif call_id is not None:
            raise ValueError("experience_call_id_unexpected")
        if retry_of is not None and event["type"] != "model_call":
            raise ValueError("experience_retry_unexpected")
        if parent_call_id is not None and parent_call_id not in call_ids:
            raise ValueError("experience_parent_call_invalid")
    if not required_types <= {event["type"] for event in events}:
        raise ValueError("experience_required_events_missing")

    outcome = _object(bundle["outcome"], "experience_outcome_invalid")
    _exact_keys(
        outcome,
        {"state", "verifier_ref", "verifier_sha256", "reward"},
        "experience_outcome_fields_invalid",
    )
    if outcome["state"] not in {"succeeded", "failed"}:
        raise ValueError("experience_outcome_not_publishable")
    _text(outcome["verifier_ref"], "experience_verifier_ref_invalid")
    _sha256(outcome["verifier_sha256"], "experience_verifier_hash_invalid")
    reward = _object(outcome["reward"], "experience_reward_invalid")
    if set(reward) != {"task"} or type(reward["task"]) not in {int, float}:
        raise ValueError("experience_reward_invalid")

    labels = _object(bundle["labels"], "experience_labels_invalid")
    _exact_keys(
        labels,
        {"success", "failure_code", "training_allowed", "annotation_refs"},
        "experience_labels_fields_invalid",
    )
    if type(labels["success"]) is not bool or labels["success"] != (
        outcome["state"] == "succeeded"
    ):
        raise ValueError("experience_success_label_mismatch")
    if labels["success"]:
        if labels["failure_code"] is not None:
            raise ValueError("experience_failure_code_unexpected")
    else:
        _text(labels["failure_code"], "experience_failure_code_missing")
    if type(labels["training_allowed"]) is not bool:
        raise ValueError("experience_training_allowed_invalid")
    if not isinstance(labels["annotation_refs"], list) or any(
        not isinstance(item, str) or not item for item in labels["annotation_refs"]
    ):
        raise ValueError("experience_annotation_refs_invalid")
    _scan_forbidden(bundle, tenant_id, allow_hidden=True)
    return deepcopy(bundle)


def publish_experience_bundle(store: EvidenceObjectStore, bundle: dict[str, Any]) -> dict[str, str]:
    """Publish a validated Experience Bundle under its canonical tenant address."""
    bundle = validate_experience_bundle(bundle)
    body = canonical_bytes(bundle)
    digest = sha256(body)
    key = f"tenants/{bundle['tenant_id']}/experiences/bundles/sha256/{digest}.json"
    _put_immutable(store, key, body)
    return {"experience_ref": key, "experience_sha256": digest}


def publish_trial_experience(
    store: EvidenceObjectStore,
    *,
    tenant_id: str,
    trial: dict[str, Any],
    transcript: dict[str, Any],
    source_manifest_ref: str,
    source_manifest_sha256: str,
) -> dict[str, str]:
    """Project one valid TVE trial into an immutable, non-trainable Experience."""
    from harness.evaluation import validate_trial_transcript

    transcript = validate_trial_transcript(transcript)
    state = trial.get("state")
    if state not in {"succeeded", "failed"}:
        raise ValueError("experience_outcome_not_publishable")
    fingerprint = _object(trial.get("fingerprint"), "experience_trial_fingerprint_missing")
    if (
        str(trial.get("run_id")) == ""
        or transcript["trial_id"] != str(trial.get("trial_id"))
        or transcript["task_bundle_id"] != fingerprint.get("task_bundle_id")
        or transcript["environment_receipt_sha256"] != fingerprint.get("environment_receipt_sha256")
    ):
        raise ValueError("experience_trial_lineage_mismatch")
    task_input_body = store.get(fingerprint["task_input_ref"])
    task_input = json.loads(task_input_body)
    if sha256(task_input_body) != fingerprint["task_input_sha256"]:
        raise ValueError("experience_task_input_hash_mismatch")

    call_id = str(uuid5(NAMESPACE_URL, f"{trial['transcript_sha256']}:model_call"))
    producer = transcript["model_fingerprint"]
    contents = [
        (
            "rollout_started",
            "h5_evaluation",
            {
                "run_id": str(trial["run_id"]),
                "trial_id": str(trial["trial_id"]),
                "task_bundle_id": transcript["task_bundle_id"],
            },
            None,
            None,
        ),
        (
            "context_built",
            "h5_evaluation",
            {
                "task_input": task_input,
                "environment_receipt_ref": transcript["environment_receipt_ref"],
                "environment_receipt_sha256": transcript["environment_receipt_sha256"],
                "actual_prompt": transcript["prompt"],
            },
            None,
            None,
        ),
        (
            "tool_call",
            "agent_c.retrieval",
            {"tool": "rag_chat", "query": task_input.get("query")},
            None,
            None,
        ),
        (
            "tool_observation",
            "agent_c.retrieval",
            {"citations": transcript["citations"], "evidence_refs": []},
            None,
            None,
        ),
        (
            "model_call",
            "h5_evaluation.model",
            {
                "schema_version": "model_call.v1",
                "request": {"messages": [{"role": "user", "content": transcript["prompt"]}]},
                "response": {"content": transcript["answer"]},
                "status": "succeeded",
                "model_fingerprint": producer,
                "generation_config": transcript["generation_policy"],
                "usage": {"value": None, "unavailable_reason": "provider_usage_not_exposed"},
                "latency_ms": transcript["latency_ms"],
                "provider_request_id": {
                    "value": None,
                    "unavailable_reason": "provider_request_id_not_exposed",
                },
                "token_ids": {
                    "value": None,
                    "unavailable_reason": "runtime_token_ids_not_captured",
                },
                "logprobs": {
                    "value": None,
                    "unavailable_reason": "runtime_logprobs_not_exposed",
                },
            },
            call_id,
            None,
        ),
        (
            "verifier_result",
            "verify_rag_outcome@1",
            transcript["verifier"],
            None,
            call_id,
        ),
        (
            "reward",
            "experience_policy@1",
            {"task": 1.0 if state == "succeeded" else 0.0},
            None,
            None,
        ),
        (
            "rollout_finished",
            "experience_policy@1",
            {
                "state": state,
                "failure_code": None
                if state == "succeeded"
                else trial.get("failure_code") or transcript["verifier"].get("error_code"),
            },
            None,
            None,
        ),
    ]
    events = []
    for sequence, (event_type, event_producer, content, event_call_id, parent_call_id) in enumerate(
        contents, 1
    ):
        _scan_forbidden(content, tenant_id, allow_hidden=True)
        event_body = canonical_bytes(content)
        event_sha256 = sha256(event_body)
        event_ref = (
            f"tenants/{tenant_id}/experiences/runs/{trial['run_id']}/events/"
            f"{event_type}/sha256/{event_sha256}.json"
        )
        _put_immutable(store, event_ref, event_body)
        events.append(
            {
                "sequence": sequence,
                "type": event_type,
                "content_ref": event_ref,
                "sha256": event_sha256,
                "producer": event_producer,
                "call_id": event_call_id,
                "retry_of": None,
                "parent_call_id": parent_call_id,
            }
        )
    verifier_event = next(event for event in events if event["type"] == "verifier_result")
    failure_code = (
        None
        if state == "succeeded"
        else (trial.get("failure_code") or transcript["verifier"].get("error_code"))
    )
    bundle = {
        "schema_version": "experience_bundle.v1",
        "tenant_id": tenant_id,
        "task_bundle_id": fingerprint["task_bundle_id"],
        "task_bundle_ref": fingerprint["task_bundle_ref"],
        "task_bundle_sha256": fingerprint["task_bundle_sha256"],
        "run_id": str(trial["run_id"]),
        "trial_id": str(trial["trial_id"]),
        "source_manifest_ref": source_manifest_ref,
        "source_manifest_sha256": source_manifest_sha256,
        "producer": {
            "model_id": producer["model_id"],
            "model_sha256": producer["model_sha256"],
            "tokenizer_sha256": producer["tokenizer_sha256"],
            "chat_template_sha256": producer["chat_template_sha256"],
            "adapter_sha256": producer["adapter_sha256"],
            "policy_sha256": transcript["generation_policy_sha256"],
        },
        "environment": {
            "receipt_ref": transcript["environment_receipt_ref"],
            "receipt_sha256": transcript["environment_receipt_sha256"],
        },
        "events": events,
        "outcome": {
            "state": state,
            "verifier_ref": verifier_event["content_ref"],
            "verifier_sha256": verifier_event["sha256"],
            "reward": {"task": 1.0 if state == "succeeded" else 0.0},
        },
        "labels": {
            "success": state == "succeeded",
            "failure_code": failure_code,
            "training_allowed": False,
            "annotation_refs": [],
        },
    }
    return publish_experience_bundle(store, bundle)
