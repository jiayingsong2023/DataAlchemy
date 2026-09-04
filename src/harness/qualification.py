"""H6 data qualification and fail-closed revocation propagation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from copy import deepcopy
from typing import Any

from harness.deployment import DeploymentBinding
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_ROLES = {"admin", "reviewer"}
_QUALIFICATION_ROLES = {
    "data_owner",
    "independent_reviewer",
    "security_approver",
    "release_decider",
}
_REQUIRED_CASE_FAMILIES = {
    "acl_and_cross_tenant",
    "conflicting_sources",
    "controlled_tool_approval",
    "grounded_qa",
    "no_evidence_abstention",
    "prompt_injection",
    "stale_source_version",
}
_REQUIRED_GATE_METRICS = {
    "abstention_rate",
    "citation_coverage",
    "citation_precision",
    "completeness",
    "context_coverage",
    "correctness",
    "cross_tenant_violations",
    "faithfulness",
    "irreversible_side_effect_violations",
    "license_or_pii_violations",
    "mrr",
    "ndcg",
    "no_evidence_hallucination_rate",
    "recall_at_10",
    "recall_at_5",
    "tool_success_rate",
}
_GATE_OPERATORS = {"eq", "gte", "gte_baseline", "lte"}


def _valid_hash(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field}_invalid")
    return value


def _json_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fields(value: Any, expected: set[str], error: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(error)
    return value


def _validate_claim(value: Any) -> None:
    claim = _fields(
        value,
        {"mode", "adapter_behavior", "final_rag_answer"},
        "qualification_manifest_claim_invalid",
    )
    modes = {"behavior_uplift_rag_no_regression", "behavior_and_rag_uplift"}
    if claim["mode"] not in modes or not all(
        isinstance(item, str) and item for item in claim.values()
    ):
        raise ValueError("qualification_manifest_claim_invalid")


def _validate_data_scope(value: Any) -> dict[str, Any]:
    fields = {
        "tenant_id",
        "source_manifest_ref",
        "source_manifest_sha256",
        "source_acl_digest",
        "permission_version",
        "data_classification",
        "allowed_purposes",
        "retention_policy",
        "deletion_policy",
        "split_policy",
    }
    data = _fields(value, fields, "qualification_manifest_data_scope_invalid")
    text_fields = fields - {"allowed_purposes", "source_manifest_sha256"}
    if any(
        data[key] is not None and (not isinstance(data[key], str) or not data[key])
        for key in text_fields
    ):
        raise ValueError("qualification_manifest_data_scope_invalid")
    if data["allowed_purposes"] != ["evaluation", "rag", "training"]:
        raise ValueError("qualification_manifest_purpose_invalid")
    if data["split_policy"] != "source_and_task_family":
        raise ValueError("qualification_manifest_split_policy_invalid")
    if data["source_manifest_sha256"] is not None:
        _valid_hash(data["source_manifest_sha256"], "qualification_source_manifest_sha256")
    return data


def _validate_suite(value: Any) -> dict[str, Any]:
    suite = _fields(
        value,
        {"version", "ref", "sha256", "case_families"},
        "qualification_manifest_suite_invalid",
    )
    families = suite["case_families"]
    valid_families = (
        isinstance(families, list)
        and len(families) == len(set(families))
        and all(isinstance(item, str) and item for item in families)
        and _REQUIRED_CASE_FAMILIES <= set(families)
    )
    if not isinstance(suite["version"], str) or not suite["version"] or not valid_families:
        raise ValueError("qualification_manifest_case_families_invalid")
    if suite["ref"] is not None and (not isinstance(suite["ref"], str) or not suite["ref"]):
        raise ValueError("qualification_manifest_suite_invalid")
    if suite["sha256"] is not None:
        _valid_hash(suite["sha256"], "qualification_suite_sha256")
    return suite


def _validate_gate(value: Any, names: set[str]) -> str:
    gate = _fields(
        value,
        {"name", "metric", "operator", "value", "hard"},
        "qualification_manifest_gate_invalid",
    )
    if (
        not isinstance(gate["name"], str)
        or not gate["name"]
        or gate["name"] in names
        or not isinstance(gate["metric"], str)
        or not gate["metric"]
        or gate["operator"] not in _GATE_OPERATORS
        or type(gate["hard"]) is not bool
    ):
        raise ValueError("qualification_manifest_gate_invalid")
    threshold = gate["value"]
    if gate["operator"] == "gte_baseline" and threshold is not None:
        raise ValueError("qualification_manifest_gate_value_invalid")
    if gate["operator"] != "gte_baseline" and (
        type(threshold) not in {int, float}
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("qualification_manifest_gate_value_invalid")
    names.add(gate["name"])
    return gate["metric"]


def _validate_gates(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("qualification_manifest_gates_missing")
    names: set[str] = set()
    metrics = {_validate_gate(gate, names) for gate in value}
    if not _REQUIRED_GATE_METRICS <= metrics:
        raise ValueError("qualification_manifest_required_gate_missing")


def _validate_slos(value: Any) -> dict[str, Any]:
    slos = _fields(
        value,
        {
            "p95_latency_ms",
            "p99_latency_ms",
            "minimum_throughput_rps",
            "candidate_to_stable_p95_ratio",
        },
        "qualification_manifest_slos_invalid",
    )
    if any(
        item is not None
        and (type(item) not in {int, float} or not math.isfinite(item) or item <= 0)
        for item in slos.values()
    ):
        raise ValueError("qualification_manifest_slos_invalid")
    return slos


def _validate_governance(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    roles = _fields(manifest["roles"], _QUALIFICATION_ROLES, "qualification_manifest_roles_invalid")
    if any(item is not None and (not isinstance(item, str) or not item) for item in roles.values()):
        raise ValueError("qualification_manifest_roles_invalid")
    expected_control = {
        "new_version_required": True,
        "post_result_threshold_changes_forbidden": True,
        "required_approvals": sorted(_QUALIFICATION_ROLES),
    }
    if manifest["change_control"] != expected_control:
        raise ValueError("qualification_manifest_change_control_invalid")
    blockers = manifest["blockers"]
    if (
        not isinstance(blockers, list)
        or len(blockers) != len(set(blockers))
        or any(not isinstance(item, str) or not item for item in blockers)
    ):
        raise ValueError("qualification_manifest_blockers_invalid")
    return roles, blockers


def _validate_frozen(
    data: dict[str, Any],
    suite: dict[str, Any],
    slos: dict[str, Any],
    roles: dict[str, Any],
    blockers: list[str],
) -> None:
    required = [
        value
        for key, value in data.items()
        if key not in {"allowed_purposes", "source_manifest_sha256"}
    ] + [suite["ref"]]
    if blockers or not all(isinstance(value, str) and value for value in required):
        raise ValueError("qualification_manifest_not_ready")
    _valid_hash(data["source_manifest_sha256"], "qualification_source_manifest_sha256")
    _valid_hash(suite["sha256"], "qualification_suite_sha256")
    if any(value is None for value in (*slos.values(), *roles.values())):
        raise ValueError("qualification_manifest_not_ready")
    if roles["data_owner"] == roles["independent_reviewer"]:
        raise ValueError("qualification_manifest_reviewer_not_independent")


def validate_qualification_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a draft or frozen ``qualification_manifest.v1`` contract."""
    fields = {
        "schema_version",
        "state",
        "version",
        "product_claim",
        "data_scope",
        "suite",
        "gates",
        "performance_slos",
        "roles",
        "change_control",
        "blockers",
    }
    manifest = _fields(manifest, fields, "qualification_manifest_fields_invalid")
    if manifest["schema_version"] != "qualification_manifest.v1":
        raise ValueError("qualification_manifest_schema_invalid")
    state, version = manifest["state"], manifest["version"]
    if state not in {"draft", "frozen"} or not isinstance(version, str) or not version:
        raise ValueError("qualification_manifest_identity_invalid")
    _validate_claim(manifest["product_claim"])
    data = _validate_data_scope(manifest["data_scope"])
    suite = _validate_suite(manifest["suite"])
    _validate_gates(manifest["gates"])
    slos = _validate_slos(manifest["performance_slos"])
    roles, blockers = _validate_governance(manifest)
    if state == "draft" and not blockers:
        raise ValueError("qualification_manifest_draft_blockers_missing")
    if state == "frozen":
        _validate_frozen(data, suite, slos, roles, blockers)
    return deepcopy(manifest)


class QualificationService:
    """Durable H6 qualification records; MinIO stores the large manifests."""

    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)

    def create(
        self,
        identity: dict[str, str],
        *,
        purpose: str,
        source_manifest_key: str,
        source_manifest_sha256: str,
        source_acl_digest: str,
        permission_version: str,
        data_classification: str,
        suite_version: str,
        suite_sha256: str,
        policy_version: str,
        retention: dict[str, Any] | None = None,
        allowed_processing: dict[str, Any] | None = None,
    ) -> str:
        if (
            not purpose
            or not source_manifest_key
            or not source_acl_digest
            or not permission_version
        ):
            raise ValueError("qualification_metadata_missing")
        if not data_classification or not suite_version or not policy_version:
            raise ValueError("qualification_policy_missing")
        _valid_hash(source_manifest_sha256, "source_manifest_sha256")
        _valid_hash(suite_sha256, "suite_sha256")
        qualification_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO qualification_records "
                    "(qualification_id, tenant_id, purpose, state, data_owner, created_by, "
                    "source_manifest_key, source_manifest_sha256, source_acl_digest, permission_version, "
                    "data_classification, retention_json, allowed_processing_json, suite_version, suite_sha256, policy_version) "
                    "VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
                    (
                        qualification_id,
                        identity["tenant_id"],
                        purpose,
                        identity["username"],
                        identity["username"],
                        source_manifest_key,
                        source_manifest_sha256,
                        source_acl_digest,
                        permission_version,
                        data_classification,
                        json.dumps(retention or {}, ensure_ascii=False),
                        json.dumps(allowed_processing or {}, ensure_ascii=False),
                        suite_version,
                        suite_sha256,
                        policy_version,
                    ),
                )
        self.audit.record(
            identity, "qualification.created", "qualification", resource_id=qualification_id
        )
        return qualification_id

    def approve_data(self, identity: dict[str, str], qualification_id: str) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, data_owner, created_by FROM qualification_records "
                    "WHERE qualification_id = %s FOR UPDATE",
                    (qualification_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("qualification_not_found")
                if row["state"] != "draft":
                    raise ValueError("qualification_not_draft")
                if identity["username"] != row["data_owner"] and identity["role"] != "admin":
                    raise PermissionError("data_owner_approval_required")
                cursor.execute(
                    "UPDATE qualification_records SET state = 'data_approved', data_approved_at = now() "
                    "WHERE qualification_id = %s",
                    (qualification_id,),
                )
        self.audit.record(
            identity, "qualification.data_approved", "qualification", resource_id=qualification_id
        )

    def mark_calibrated(
        self,
        identity: dict[str, str],
        qualification_id: str,
        *,
        base_evaluation_id: str,
        candidate_evaluation_id: str,
        calibration_report_key: str,
        calibration_report_sha256: str,
    ) -> None:
        self._reviewer(identity)
        _valid_hash(calibration_report_sha256, "calibration_report_sha256")
        if not calibration_report_key:
            raise ValueError("calibration_report_key_missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, created_by, suite_version, suite_sha256, policy_version "
                    "FROM qualification_records WHERE qualification_id = %s FOR UPDATE",
                    (qualification_id,),
                )
                qualification = cursor.fetchone()
                if qualification is None:
                    raise ValueError("qualification_not_found")
                if qualification["state"] != "data_approved":
                    raise ValueError("qualification_data_approval_required")
                if qualification["created_by"] == identity["username"]:
                    raise PermissionError("creator_cannot_calibrate")
                cursor.execute(
                    "SELECT evaluation_id, tenant_id, suite_version, suite_sha256, policy_version, state "
                    "FROM evaluation_campaigns WHERE evaluation_id = ANY(%s)",
                    ([base_evaluation_id, candidate_evaluation_id],),
                )
                evaluations = {str(row["evaluation_id"]): row for row in cursor.fetchall()}
                base = evaluations.get(base_evaluation_id)
                candidate = evaluations.get(candidate_evaluation_id)
                if base is None or candidate is None:
                    raise ValueError("qualification_evaluation_missing")
                if (
                    base["tenant_id"] != identity["tenant_id"]
                    or candidate["tenant_id"] != identity["tenant_id"]
                ):
                    raise PermissionError("qualification_evaluation_tenant_mismatch")
                for row in (base, candidate):
                    if row["state"] != "passed":
                        raise ValueError("qualification_evaluation_not_passed")
                    if (
                        row["suite_version"] != qualification["suite_version"]
                        or row["suite_sha256"] != qualification["suite_sha256"]
                        or row["policy_version"] != qualification["policy_version"]
                    ):
                        raise ValueError("qualification_evaluation_policy_mismatch")
                cursor.execute(
                    "UPDATE qualification_records SET state = 'calibrated', reviewer = %s, "
                    "base_evaluation_id = %s, candidate_evaluation_id = %s, calibration_report_key = %s, "
                    "calibration_report_sha256 = %s, calibrated_at = now() WHERE qualification_id = %s",
                    (
                        identity["username"],
                        base_evaluation_id,
                        candidate_evaluation_id,
                        calibration_report_key,
                        calibration_report_sha256,
                        qualification_id,
                    ),
                )
        self.audit.record(
            identity,
            "qualification.calibrated",
            "qualification",
            resource_id=qualification_id,
            metadata={
                "base_evaluation_id": base_evaluation_id,
                "candidate_evaluation_id": candidate_evaluation_id,
            },
        )

    def mark_pilot_ready(
        self,
        identity: dict[str, str],
        qualification_id: str,
        *,
        stable_release_id: str,
        candidate_release_id: str,
        deployment_evidence_key: str,
        deployment_evidence_sha256: str,
    ) -> None:
        self._reviewer(identity)
        _valid_hash(deployment_evidence_sha256, "deployment_evidence_sha256")
        if not deployment_evidence_key:
            raise ValueError("deployment_evidence_key_missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state FROM qualification_records WHERE qualification_id = %s FOR UPDATE",
                    (qualification_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("qualification_not_found")
                if row["state"] != "calibrated":
                    raise ValueError("qualification_calibration_required")
                cursor.execute(
                    "SELECT release_id, status, manifest_json FROM release_records "
                    "WHERE release_id = ANY(%s) AND tenant_id = %s",
                    ([stable_release_id, candidate_release_id], identity["tenant_id"]),
                )
                releases = {str(item["release_id"]): item for item in cursor.fetchall()}
                stable = releases.get(stable_release_id)
                candidate = releases.get(candidate_release_id)
                if stable is None or candidate is None:
                    raise ValueError("qualification_release_missing")
                if stable_release_id == candidate_release_id:
                    raise ValueError("qualification_release_pair_invalid")
                if stable["status"] != "promoted" or candidate["status"] not in {
                    "canary",
                    "promoted",
                }:
                    raise ValueError("qualification_release_window_incomplete")
                try:
                    binding = DeploymentBinding.from_manifest(candidate["manifest_json"])
                except (TypeError, ValueError) as error:
                    raise ValueError("qualification_deployment_binding_invalid") from error
                if (
                    binding.stable_release_id != stable_release_id
                    or binding.candidate_release_id != candidate_release_id
                ):
                    raise ValueError("qualification_deployment_release_mismatch")
                cursor.execute(
                    "UPDATE qualification_records SET state = 'pilot_ready', stable_release_id = %s, "
                    "candidate_release_id = %s, deployment_evidence_key = %s, deployment_evidence_sha256 = %s, "
                    "pilot_ready_at = now() WHERE qualification_id = %s",
                    (
                        stable_release_id,
                        candidate_release_id,
                        deployment_evidence_key,
                        deployment_evidence_sha256,
                        qualification_id,
                    ),
                )
        self.audit.record(
            identity, "qualification.pilot_ready", "qualification", resource_id=qualification_id
        )

    def revoke(self, identity: dict[str, str], qualification_id: str, reason: str) -> None:
        self._reviewer(identity)
        if not reason:
            raise ValueError("qualification_revoke_reason_missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, candidate_evaluation_id FROM qualification_records "
                    "WHERE qualification_id = %s FOR UPDATE",
                    (qualification_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("qualification_not_found")
                if row["state"] == "revoked":
                    return
                cursor.execute(
                    "UPDATE qualification_records SET state = 'revoked', reason = %s, revoked_at = now() "
                    "WHERE qualification_id = %s",
                    (reason, qualification_id),
                )
                cursor.execute(
                    "UPDATE trajectory_annotations SET status = 'revoked', training_allowed = false, "
                    "reason = %s, reviewed_at = now() WHERE annotation_id IN "
                    "(SELECT i.source_id FROM training_snapshot_items i JOIN training_snapshots s "
                    "ON s.snapshot_id = i.snapshot_id WHERE s.qualification_id = %s)",
                    (reason, qualification_id),
                )
                cursor.execute(
                    "UPDATE training_snapshots SET state = 'revoked', revoke_reason = %s "
                    "WHERE qualification_id = %s AND state <> 'revoked'",
                    (reason, qualification_id),
                )
                cursor.execute(
                    "UPDATE adapter_manifests SET state = 'revoked', revoked_at = now(), revoke_reason = %s "
                    "WHERE snapshot_id IN (SELECT snapshot_id FROM training_snapshots WHERE qualification_id = %s) "
                    "AND state <> 'revoked'",
                    (reason, qualification_id),
                )
                cursor.execute(
                    "UPDATE release_records SET status = 'rolled_back', updated_at = now(), version = version + 1 "
                    "WHERE qualification_id = %s AND status IN ('candidate', 'shadow', 'canary', 'promoted')",
                    (qualification_id,),
                )
                if row["candidate_evaluation_id"]:
                    cursor.execute(
                        "UPDATE training_snapshots SET state = 'revoked', revoke_reason = %s "
                        "WHERE snapshot_id IN (SELECT snapshot_id FROM adapter_manifests WHERE evaluation_id = %s) "
                        "AND state <> 'revoked'",
                        (reason, row["candidate_evaluation_id"]),
                    )
                    cursor.execute(
                        "UPDATE adapter_manifests SET state = 'revoked', revoked_at = now(), revoke_reason = %s "
                        "WHERE evaluation_id = %s AND state <> 'revoked'",
                        (reason, row["candidate_evaluation_id"]),
                    )
        self.audit.record(
            identity,
            "qualification.revoked",
            "qualification",
            resource_id=qualification_id,
            metadata={"reason": reason},
        )

    def get(self, identity: dict[str, str], qualification_id: str) -> dict[str, Any] | None:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM qualification_records WHERE qualification_id = %s",
                    (qualification_id,),
                )
                row = cursor.fetchone()
        return self._normalize(row)

    def list(self, identity: dict[str, str], limit: int = 100) -> list[dict[str, Any]]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM qualification_records ORDER BY created_at DESC LIMIT %s",
                    (min(max(limit, 1), 500),),
                )
                rows = cursor.fetchall()
        return [self._normalize(row) for row in rows]

    @staticmethod
    def _normalize(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in (
            "qualification_id",
            "base_evaluation_id",
            "candidate_evaluation_id",
            "stable_release_id",
            "candidate_release_id",
        ):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result

    @staticmethod
    def _reviewer(identity: dict[str, str]) -> None:
        if identity.get("role") not in _REVIEW_ROLES:
            raise PermissionError("Reviewer role required")
