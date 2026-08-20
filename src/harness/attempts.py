"""Durable state for the two-stage PDF/H5 entrypoint."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from storage.postgres import PostgresDatabase


def config_sha256(config: dict[str, Any]) -> str:
    body = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AttemptBusy(RuntimeError):
    """Another process owns the attempt lease."""


class H5AttemptStore:
    """Small database-backed attempt/lease store; no second workflow engine."""

    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)

    def create_or_load(
        self,
        identity: dict[str, str],
        run_id: str,
        config: dict[str, Any],
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        digest = config_sha256(config)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                if attempt_id:
                    cursor.execute(
                        "SELECT * FROM h5_attempts "
                        "WHERE attempt_id = %s AND tenant_id = %s FOR UPDATE",
                        (attempt_id, identity["tenant_id"]),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise PermissionError("h5_attempt_not_found")
                    if str(row["run_id"]) != str(run_id) or row["config_sha256"] != digest:
                        raise ValueError("h5_attempt_config_mismatch")
                    return dict(row)
                cursor.execute(
                    "SELECT * FROM h5_attempts "
                    "WHERE run_id = %s AND tenant_id = %s AND active FOR UPDATE",
                    (run_id, identity["tenant_id"]),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row["config_sha256"] != digest:
                        raise ValueError("h5_active_attempt_config_mismatch")
                    return dict(row)
                attempt_id = str(uuid.uuid4())
                cursor.execute(
                    "INSERT INTO h5_attempts "
                    "(attempt_id, run_id, tenant_id, state, config_json, config_sha256, "
                    "created_by) "
                    "VALUES (%s, %s, %s, 'candidate', %s::jsonb, %s, %s) RETURNING *",
                    (
                        attempt_id,
                        run_id,
                        identity["tenant_id"],
                        json.dumps(config, ensure_ascii=False, sort_keys=True),
                        digest,
                        identity["username"],
                    ),
                )
                return dict(cursor.fetchone())

    def get(self, identity: dict[str, str], attempt_id: str) -> dict[str, Any]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM h5_attempts WHERE attempt_id = %s AND tenant_id = %s",
                    (attempt_id, identity["tenant_id"]),
                )
                row = cursor.fetchone()
        if row is None:
            raise PermissionError("h5_attempt_not_found")
        return dict(row)

    def active(self, identity: dict[str, str], run_id: str) -> dict[str, Any] | None:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM h5_attempts WHERE run_id = %s AND tenant_id = %s "
                    "AND active ORDER BY created_at DESC LIMIT 2",
                    (run_id, identity["tenant_id"]),
                )
                rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple_active_h5_attempts")
        return dict(rows[0]) if rows else None

    def acquire(
        self, identity: dict[str, str], attempt_id: str, owner: str, ttl_seconds: int = 300
    ) -> None:
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE h5_attempts SET lease_owner = %s, lease_expires_at = %s, "
                    "updated_at = now() WHERE attempt_id = %s AND tenant_id = %s AND active "
                    "AND (lease_owner IS NULL OR lease_expires_at < now() OR lease_owner = %s)",
                    (owner, expires, attempt_id, identity["tenant_id"], owner),
                )
                if cursor.rowcount != 1:
                    raise AttemptBusy("already_running")

    def renew(
        self, identity: dict[str, str], attempt_id: str, owner: str, ttl_seconds: int = 300
    ) -> None:
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE h5_attempts SET lease_expires_at = %s, updated_at = now() "
                    "WHERE attempt_id = %s AND tenant_id = %s AND active AND lease_owner = %s",
                    (expires, attempt_id, identity["tenant_id"], owner),
                )
                if cursor.rowcount != 1:
                    raise AttemptBusy("h5_attempt_lease_lost")

    def state(
        self,
        identity: dict[str, str],
        attempt_id: str,
        state: str,
        *,
        gate: str | None = None,
        error_code: str | None = None,
    ) -> None:
        active = state not in {"passed", "failed", "rolled_back", "cancelled"}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE h5_attempts SET state = %s, active = %s, "
                    "lease_owner = CASE WHEN %s THEN lease_owner ELSE NULL END, "
                    "lease_expires_at = CASE WHEN %s THEN lease_expires_at ELSE NULL END, "
                    "last_gate = %s, last_error_code = %s, "
                    "updated_at = now() WHERE attempt_id = %s AND tenant_id = %s",
                    (
                        state,
                        active,
                        active,
                        active,
                        gate,
                        error_code,
                        attempt_id,
                        identity["tenant_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("h5_attempt_not_found")

    def refs(
        self,
        identity: dict[str, str],
        attempt_id: str,
        **values: str | None,
    ) -> None:
        allowed = {
            "snapshot_id",
            "base_evaluation_id",
            "candidate_evaluation_id",
            "adapter_id",
            "release_id",
        }
        if not values or set(values) - allowed:
            raise ValueError("h5_attempt_reference_invalid")
        assignments = ", ".join(f"{key} = %s" for key in values)
        params = [values[key] for key in values]
        params.extend([attempt_id, identity["tenant_id"]])
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE h5_attempts SET {assignments}, updated_at = now() "
                    "WHERE attempt_id = %s AND tenant_id = %s",
                    params,
                )
                if cursor.rowcount != 1:
                    raise PermissionError("h5_attempt_not_found")

    def gate(
        self,
        identity: dict[str, str],
        run_id: str,
        attempt_id: str,
        gate_name: str,
        state: str,
        *,
        input_artifact_id: str | None = None,
        input_sha256: str | None = None,
        output_artifact_id: str | None = None,
        output_sha256: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO run_gate_events "
                    "(gate_event_id, run_id, tenant_id, attempt_id, gate_name, state, "
                    "input_artifact_id, input_sha256, output_artifact_id, output_sha256, "
                    "evidence_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        event_id,
                        run_id,
                        identity["tenant_id"],
                        attempt_id,
                        gate_name,
                        state,
                        input_artifact_id,
                        input_sha256,
                        output_artifact_id,
                        output_sha256,
                        json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    ),
                )
        return event_id
