"""Shared contracts for deterministic, read-only verifiers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class VerificationResult:
    status: str
    summary: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class VerifierSpec:
    name: str
    version: int
    handler: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any], "ReadOnlyServices"], VerificationResult
    ]
    timeout_seconds: float = 30.0
    max_attempts: int = 2

    @property
    def contract_digest(self) -> str:
        return _digest(
            {
                "name": self.name,
                "version": self.version,
                "timeout_seconds": self.timeout_seconds,
                "max_attempts": self.max_attempts,
            }
        )


class ReadOnlyServices:
    """Verifier-only PostgreSQL reads. The transaction rejects writes server-side."""

    def __init__(self, database_url: str, identity: dict[str, str]):
        self.database = PostgresDatabase(database_url)
        self.identity = identity

    def documents(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.document_id, d.source_uri, d.content_hash, d.version, d.status, d.metadata_json, "
                    "count(c.chunk_id) AS chunk_count "
                    "FROM documents d LEFT JOIN document_chunks c ON c.document_id = d.document_id "
                    "WHERE d.document_id = ANY(%s) GROUP BY d.document_id",
                    (document_ids,),
                )
                return [
                    {
                        **row,
                        "document_id": str(row["document_id"]),
                        "metadata": row.pop("metadata_json"),
                    }
                    for row in cursor.fetchall()
                ]

    @staticmethod
    def _object_parts(key: str) -> tuple[S3Utils, str]:
        normalized = key.replace("s3a://", "s3://", 1)
        if normalized.startswith("s3://"):
            bucket, _, object_key = normalized.removeprefix("s3://").partition("/")
            return S3Utils(bucket=bucket), object_key
        return S3Utils(), normalized

    def object_body(self, key: str) -> bytes | None:
        store, object_key = self._object_parts(key)
        return store.get_object_body(object_key)

    def object_json(self, key: str) -> Any:
        body = self.object_body(key)
        if body is None:
            return None
        return json.loads(body)

    def object_records(self, prefix: str) -> list[dict[str, Any]]:
        store, object_prefix = self._object_parts(prefix)
        records: list[dict[str, Any]] = []
        for item in sorted(
            store.list_objects(object_prefix.rstrip("/") + "/"), key=lambda value: value["Key"]
        ):
            if not item["Key"].endswith((".json", ".jsonl")):
                continue
            body = store.get_object_body(item["Key"])
            if body is None:
                continue
            records.extend(
                json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()
            )
        return records

    def matching_chunks(self, document_id: str, query: str) -> int:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS count FROM document_chunks "
                    "WHERE document_id = %s AND position(lower(%s) in lower(text)) > 0",
                    (document_id, query),
                )
                return int(cursor.fetchone()["count"])

    def chunks(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT chunk_id, document_id, ordinal, metadata_json FROM document_chunks "
                    "WHERE document_id = ANY(%s) ORDER BY document_id, ordinal",
                    (document_ids,),
                )
                return [
                    {
                        **row,
                        "chunk_id": str(row["chunk_id"]),
                        "document_id": str(row["document_id"]),
                        "metadata": row.pop("metadata_json"),
                    }
                    for row in cursor.fetchall()
                ]

    def memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, status, source_event_id, valid_until, content_hash FROM memories "
                    "WHERE memory_id = %s",
                    (memory_id,),
                )
                return cursor.fetchone()

    def context_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, tenant_id, identity_digest, pack_refs, budget_json, envelope_sha256 "
                    "FROM context_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                return cursor.fetchone()

    def context_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT checkpoint_id, session_id, tenant_id, source_sequence_start, source_sequence_end, source_digest, "
                    "summary, handoff_json, status FROM context_checkpoints WHERE checkpoint_id = %s",
                    (checkpoint_id,),
                )
                return cursor.fetchone()

    def conversation_events(
        self, session_id: str, sequence_start: int, sequence_end: int
    ) -> list[dict[str, Any]]:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, sequence_no, content_sha256 FROM conversation_events "
                    "WHERE session_id = %s AND sequence_no BETWEEN %s AND %s ORDER BY sequence_no",
                    (session_id, sequence_start, sequence_end),
                )
                return [{**row, "event_id": str(row["event_id"])} for row in cursor.fetchall()]

    def memory_candidate(self, memory_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, tenant_id, status, scope_type, scope_id, claim_key, confidence, "
                    "trust_label, risk_class, sensitivity_label, policy_version FROM memories WHERE memory_id = %s",
                    (memory_id,),
                )
                return cursor.fetchone()

    def release(self, release_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT release_id, tenant_id, status, manifest_json, release_scope, adapter_id, "
                    "evaluation_id, training_snapshot_id, rollback_release_id, version "
                    "FROM release_records WHERE release_id = %s",
                    (release_id,),
                )
                return cursor.fetchone()

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT evaluation_id, tenant_id, subject_type, subject_ref, suite_version, suite_sha256, "
                    "policy_version, required_trials, state, baseline_evaluation_id, metrics_json, hard_gates_json "
                    "FROM evaluation_campaigns WHERE evaluation_id = %s",
                    (evaluation_id,),
                )
                row = cursor.fetchone()
        if row:
            row["metrics"] = row.pop("metrics_json")
            row["hard_gates"] = row.pop("hard_gates_json")
        return row

    def qualification(self, qualification_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT qualification_id, tenant_id, purpose, state, data_owner, created_by, reviewer, "
                    "source_manifest_key, source_manifest_sha256, source_acl_digest, permission_version, "
                    "data_classification, suite_version, suite_sha256, policy_version, base_evaluation_id, "
                    "candidate_evaluation_id, calibration_report_key, calibration_report_sha256, stable_release_id, "
                    "candidate_release_id, deployment_evidence_key, deployment_evidence_sha256, reason "
                    "FROM qualification_records WHERE qualification_id = %s",
                    (qualification_id,),
                )
                row = cursor.fetchone()
        return row

    def trial(self, trial_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT trial_id, evaluation_id, run_id, task_id, tenant_id, case_id, trial_no, state, "
                    "fingerprint_json, outcome_json, metrics_json, failure_code, transcript_key, "
                    "transcript_sha256 FROM trajectory_trials "
                    "WHERE trial_id = %s",
                    (trial_id,),
                )
                row = cursor.fetchone()
        if row:
            row["fingerprint"] = row.pop("fingerprint_json")
            row["outcome"] = row.pop("outcome_json")
            row["metrics"] = row.pop("metrics_json")
        return row

    def run_manifest(self, run_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, task_id, tenant_id, state, final_outcome, object_key, "
                    "manifest_sha256 FROM run_manifests WHERE run_id = %s",
                    (run_id,),
                )
                row = cursor.fetchone()
        if row:
            row["run_id"] = str(row["run_id"])
            row["task_id"] = str(row["task_id"])
        return row

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, tenant_id, state, dataset_key, dataset_sha256, dataset_size, "
                    "policy_version, split_json, base_model_digest, created_by, approved_by, algorithm, "
                    "compile_manifest_key, compile_manifest_sha256, target_tokenizer_digest, chat_template_digest "
                    "FROM training_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "SELECT item_id, split, source_type, source_id, source_tenant_id, source_sha256, "
                        "training_allowed, training_purpose, training_permission_version "
                        "FROM training_snapshot_items WHERE snapshot_id = %s ORDER BY item_id",
                        (snapshot_id,),
                    )
                    row["items"] = cursor.fetchall()
        return row

    def annotation(self, annotation_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT annotation_id, trial_id, run_id, tenant_id, label_json, content_key, "
                    "content_sha256, source_acl_digest, status, training_allowed, training_purpose, "
                    "training_permission_version FROM trajectory_annotations WHERE annotation_id = %s",
                    (annotation_id,),
                )
                row = cursor.fetchone()
        if row:
            row["annotation_id"] = str(row["annotation_id"])
            row["trial_id"] = str(row["trial_id"]) if row["trial_id"] else None
            row["run_id"] = str(row["run_id"])
            row["label"] = row.pop("label_json")
        return row

    def adapter(self, adapter_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT adapter_id, tenant_id, snapshot_id, base_model_digest, tokenizer_digest, artifact_key, "
                    "artifact_sha256, artifact_size, config_json, safety_scan_json, evaluation_id, state "
                    "FROM adapter_manifests WHERE adapter_id = %s",
                    (adapter_id,),
                )
                row = cursor.fetchone()
        return row

    def job(self, task_id: str, step_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, input_key, input_sha256, result_sha256, error_code "
                    "FROM agent_jobs WHERE task_id = %s AND step_id = %s",
                    (task_id, step_id),
                )
                return cursor.fetchone()


class VerifierRegistry:
    def __init__(self):
        self._specs: dict[tuple[str, int], VerifierSpec] = {}

    def register(self, spec: VerifierSpec) -> None:
        key = (spec.name, spec.version)
        if key in self._specs:
            raise ValueError(f"Verifier {spec.name}@{spec.version} already registered")
        if spec.version < 1 or spec.timeout_seconds <= 0 or spec.max_attempts < 1:
            raise ValueError("Verifier version, timeout and attempts must be positive")
        self._specs[key] = spec

    def get(self, name: str, version: int) -> VerifierSpec:
        try:
            return self._specs[(name, version)]
        except KeyError as error:
            raise ValueError(f"Unknown verifier: {name}@{version}") from error
