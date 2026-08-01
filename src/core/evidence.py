"""Canonical, tenant-scoped evidence manifests for harness runs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from storage.postgres import PostgresDatabase

MAX_MANIFEST_BYTES = 1_048_576


class ObjectNotFound(Exception):
    """The only storage error that may be treated as an absent object."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def sha256(value: bytes | dict[str, Any]) -> str:
    data = canonical_bytes(value) if isinstance(value, dict) else value
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvidenceObjectStore(Protocol):
    def put(self, key: str, body: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def copy(self, source: str, target: str) -> None: ...

    def delete(self, key: str) -> None: ...


class S3EvidenceStore:
    """Strict S3 operations; unlike legacy helpers, failures are never hidden."""

    def __init__(self, bucket: str, client: Any):
        self.bucket = bucket
        self.client = client

    def put(self, key: str, body: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={"sha256": sha256(body)},
        )

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise ObjectNotFound(key) from error
            raise

    def copy(self, source: str, target: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=target,
            CopySource={"Bucket": self.bucket, "Key": source},
            MetadataDirective="COPY",
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def fingerprint() -> dict[str, Any]:
    """Record only build/runtime identifiers, never credentials or prompt content."""

    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    return {
        "source_revision": {"value": os.getenv("BUILD_GIT_SHA"), "source": "env"},
        "image_digest": {"value": os.getenv("IMAGE_DIGEST"), "source": "env"},
        "dependency_lock": {
            "value": sha256(lock.read_bytes()) if lock.exists() else None,
            "source": "uv.lock",
        },
        "context_skill": {"value": None, "source": "h4", "availability": "not_configured"},
    }


def project_tool_result(
    result: dict[str, Any], sensitivity: dict[str, str] | None = None
) -> dict[str, Any]:
    """Keep only typed, non-secret evidence fields from an immutable ToolResult."""

    sensitivity = sensitivity or {}
    output: dict[str, Any] = {}
    for key, value in result.get("output", {}).items():
        level = sensitivity.get(key, sensitivity.get("*"))
        if level == "public":
            output[key] = value
        elif level == "internal":
            output[key] = {"digest": sha256(canonical_bytes(value))}
        elif level == "secret":
            continue
        else:
            raise ValueError(f"unclassified_result_field:{key}")
    return {
        "schema_version": result.get("schema_version"),
        "status": result.get("status"),
        "tool": result.get("tool"),
        "input_refs": result.get("input_refs", []),
        "observed_scope": result.get("observed_scope", []),
        "output": output,
        "artifacts": [
            {
                key: artifact[key]
                for key in ("store", "kind", "id", "version", "sha256", "size", "content_type")
                if key in artifact
            }
            for artifact in result.get("artifacts", [])
        ],
        "metrics": result.get("metrics", {}),
        "operation_ref": result.get("operation_ref"),
        "log_ref": result.get("log_ref"),
        "failure": result.get("failure"),
        "recorded_at": result.get("recorded_at"),
    }


@dataclass
class EvidenceService:
    database_url: str
    store: EvidenceObjectStore
    sensitivity_for: Callable[[str], dict[str, str]] = lambda _tool: {}

    @property
    def database(self) -> PostgresDatabase:
        return PostgresDatabase(self.database_url)

    def request(self, task: dict[str, Any], identity: dict[str, str], outcome: str) -> None:
        """Durably request publication before an H2 success is exposed."""

        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO run_manifests (run_id, task_id, tenant_id, state, final_outcome) "
                    "VALUES (%s, %s, %s, 'requested', %s) "
                    "ON CONFLICT (run_id) DO NOTHING",
                    (task["run_id"], task["task_id"], task["tenant_id"], outcome),
                )
                cursor.execute(
                    "INSERT INTO harness_outbox (outbox_id, tenant_id, run_id, task_id, kind, dedupe_key) "
                    "VALUES (%s, %s, %s, %s, 'publish_manifest', %s) ON CONFLICT (dedupe_key) DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        task["tenant_id"],
                        task["run_id"],
                        task["task_id"],
                        f"manifest:{task['run_id']}",
                    ),
                )

    def snapshot(self, task: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
        """Build a repeatable, redacted snapshot from the existing H0/H1 facts."""

        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_type, payload_json, occurred_at FROM agent_events "
                    "WHERE task_id = %s ORDER BY occurred_at, event_id",
                    (task["task_id"],),
                )
                events = cursor.fetchall()
                cursor.execute(
                    "SELECT tool_name, result_json, step_id, plan_version FROM agent_tool_runs "
                    "WHERE task_id = %s ORDER BY plan_version, step_id",
                    (task["task_id"],),
                )
                tool_runs = cursor.fetchall()
                cursor.execute(
                    "SELECT step_id, criterion_id, verifier, verifier_version, verifier_contract_digest, "
                    "attempt, status, tool_result_digest, error_code, summary_json, completed_at "
                    "FROM agent_step_verifications WHERE task_id = %s "
                    "ORDER BY step_id, criterion_id, attempt",
                    (task["task_id"],),
                )
                verifications = cursor.fetchall()
                cursor.execute(
                    "SELECT job_id, step_id, kind, backend, state, external_name, attempt, input_key, "
                    "input_sha256, result_key, result_sha256, log_key, log_sha256, error_code "
                    "FROM agent_jobs WHERE task_id = %s ORDER BY step_id",
                    (task["task_id"],),
                )
                jobs = cursor.fetchall()
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                migrations = [row["version"] for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT final_outcome FROM run_manifests WHERE run_id = %s", (task["run_id"],)
                )
                manifest_row = cursor.fetchone()
        steps: list[dict[str, Any]] = []
        for row in tool_runs:
            result = row["result_json"]
            if isinstance(result, str):
                result = json.loads(result)
            result = result or {}
            projected = project_tool_result(result, self.sensitivity_for(row["tool_name"]))
            steps.append(
                {
                    "step_id": str(row["step_id"]),
                    "plan_version": row["plan_version"],
                    "tool_result": projected,
                    "tool_result_digest": sha256(canonical_bytes(result)),
                }
            )
        timeline = []
        for row in events:
            payload = row["payload_json"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            timeline.append(
                {
                    "event_type": row["event_type"],
                    "occurred_at": row["occurred_at"].isoformat(),
                    "run_id": payload.get("run_id"),
                    "step_id": payload.get("step_id"),
                    "criterion_id": payload.get("criterion_id"),
                    "error_code": payload.get("error_code"),
                }
            )
        outcome = (manifest_row or {}).get("final_outcome") or task["state"]
        if os.getenv("DATAALCHEMY_ENV", "development").lower() != "production":
            outcome = "development_evidence"
        return {
            "schema_version": 1,
            "run": {
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "tenant_id": task["tenant_id"],
                "outcome": outcome,
                "finish_reason": task.get("finish_reason"),
            },
            "task_contract": {
                "task_spec": task["task_spec"],
                "task_spec_digest": sha256(canonical_bytes(task["task_spec"])),
                "final_plan": task["plan"],
                "final_plan_digest": sha256(canonical_bytes(task["plan"])),
            },
            "steps": steps,
            "verifications": [
                {
                    **{key: str(row[key]) for key in ("step_id",) if row[key] is not None},
                    **{key: row[key] for key in row if key != "step_id" and key != "summary_json"},
                    "summary": row["summary_json"],
                }
                for row in verifications
            ],
            "jobs": [
                {**row, "job_id": str(row["job_id"]), "step_id": str(row["step_id"])}
                for row in jobs
            ],
            "timeline": timeline,
            "fingerprint": {**fingerprint(), "migrations": migrations},
            "integrity": {"source_snapshot_completed_at": _now(), "redaction_policy_version": 1},
        }

    def publish(self, task: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
        """Idempotently stage, verify and publish one run's canonical manifest."""

        manifest = self.snapshot(task, identity)
        if os.getenv("DATAALCHEMY_ENV", "development").lower() == "production":
            required = ("source_revision", "image_digest", "dependency_lock")
            if any(not manifest["fingerprint"][item]["value"] for item in required):
                raise ValueError("fingerprint_incomplete")
        body = canonical_bytes(manifest)
        if len(body) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest_too_large")
        digest = sha256(body)
        base = f"evidence/{task['tenant_id']}/{task['run_id']}"
        staging = f"{base}/staging/{digest}.json"
        final = f"{base}/manifests/sha256/{digest}.json"
        self.store.put(staging, body)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE run_manifests SET state = 'staged', staging_key = %s, attempt = attempt + 1, "
                    "staged_at = now() WHERE run_id = %s",
                    (staging, task["run_id"]),
                )
        if sha256(self.store.get(staging)) != digest:
            raise RuntimeError("staging_hash_mismatch")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE run_manifests SET state = 'verified', verified_at = now() WHERE run_id = %s",
                    (task["run_id"],),
                )
        try:
            existing = self.store.get(final)
        except ObjectNotFound:
            self.store.copy(staging, final)
        else:
            if sha256(existing) != digest:
                raise RuntimeError("published_key_conflict")
        if sha256(self.store.get(final)) != digest:
            raise RuntimeError("published_hash_mismatch")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE run_manifests SET state = 'published', staging_key = NULL, object_key = %s, "
                    "manifest_sha256 = %s, manifest_size = %s, fingerprint_digest = %s, attempt = attempt + 1, "
                    "published_at = now(), last_error_code = NULL WHERE run_id = %s",
                    (
                        final,
                        digest,
                        len(body),
                        sha256(canonical_bytes(manifest["fingerprint"])),
                        task["run_id"],
                    ),
                )
                cursor.execute(
                    "UPDATE harness_outbox SET state = 'completed', completed_at = now(), lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE dedupe_key = %s",
                    (f"manifest:{task['run_id']}",),
                )
        return {"object_key": final, "sha256": digest, "size": len(body)}

    def reconcile(self, task: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
        """Claim the durable outbox entry, then publish or retain a diagnosable failure."""

        dedupe_key = f"manifest:{task['run_id']}"
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE harness_outbox SET state = 'processing', attempt = attempt + 1, "
                    "lease_owner = %s, lease_expires_at = now() + interval '60 seconds' "
                    "WHERE dedupe_key = %s AND state IN ('pending', 'retry') RETURNING outbox_id",
                    (f"evidence:{os.getpid()}", dedupe_key),
                )
                claimed = cursor.fetchone()
        if claimed is None:
            with self.database.transaction(identity, read_only=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT state FROM run_manifests WHERE run_id = %s", (task["run_id"],)
                    )
                    row = cursor.fetchone()
            if row and row["state"] == "published":
                return {"already_published": True}
            raise RuntimeError("evidence_reconcile_busy")
        try:
            return self.publish(task, identity)
        except (RuntimeError, ValueError) as error:
            with self.database.transaction(identity) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE run_manifests SET state = 'publish_blocked', last_error_code = %s "
                        "WHERE run_id = %s",
                        (str(error), task["run_id"]),
                    )
                    cursor.execute(
                        "UPDATE harness_outbox SET state = 'retry', available_at = now() + interval '30 seconds', "
                        "lease_owner = NULL, lease_expires_at = NULL, last_error_code = %s WHERE dedupe_key = %s",
                        (str(error), dedupe_key),
                    )
            raise

    def delete_manifest(self, task: dict[str, Any], identity: dict[str, str]) -> None:
        """Delete only a discovered, tenant-authorized published object, then tombstone its index."""

        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT object_key FROM run_manifests WHERE run_id = %s FOR UPDATE",
                    (task["run_id"],),
                )
                row = cursor.fetchone()
                if row is None or not row["object_key"]:
                    raise PermissionError("Manifest not found")
                cursor.execute(
                    "INSERT INTO harness_outbox (outbox_id, tenant_id, run_id, task_id, kind, dedupe_key) "
                    "VALUES (%s, %s, %s, %s, 'delete_manifest', %s) ON CONFLICT (dedupe_key) DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        task["tenant_id"],
                        task["run_id"],
                        task["task_id"],
                        f"delete:{task['run_id']}",
                    ),
                )
                cursor.execute(
                    "UPDATE run_manifests SET state = 'deleting' WHERE run_id = %s",
                    (task["run_id"],),
                )
        self.store.delete(row["object_key"])
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE run_manifests SET state = 'deleted', deleted_at = now(), staging_key = NULL "
                    "WHERE run_id = %s",
                    (task["run_id"],),
                )
                cursor.execute(
                    "UPDATE harness_outbox SET state = 'completed', completed_at = now() WHERE dedupe_key = %s",
                    (f"delete:{task['run_id']}",),
                )

    def manifest(self, run_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM run_manifests WHERE run_id = %s", (run_id,))
                row = cursor.fetchone()
        if row is None or row["state"] != "published":
            raise PermissionError("Published manifest not found")
        body = self.store.get(row["object_key"])
        if sha256(body) != row["manifest_sha256"]:
            raise RuntimeError("manifest_corrupt")
        return json.loads(body)
