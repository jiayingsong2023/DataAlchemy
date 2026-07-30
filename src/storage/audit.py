"""Tenant-scoped, redacted audit records for governed operations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from storage.postgres import PostgresDatabase

SENSITIVE_FIELDS = frozenset({"authorization", "password", "secret", "token", "api_key"})


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in SENSITIVE_FIELDS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditLog:
    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)

    def record(
        self,
        identity: dict[str, str],
        action: str,
        resource_type: str,
        outcome: str = "allowed",
        resource_id: str | None = None,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if outcome not in {"allowed", "denied", "failed"}:
            raise ValueError("Unsupported audit outcome")
        audit_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO audit_events "
                    "(audit_id, tenant_id, actor, action, resource_type, resource_id, outcome, "
                    "correlation_id, metadata_json, occurred_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        audit_id,
                        identity["tenant_id"],
                        identity["username"],
                        action,
                        resource_type,
                        resource_id,
                        outcome,
                        correlation_id,
                        json.dumps(redact(metadata or {}), ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        return audit_id

    def list(self, identity: dict[str, str], limit: int = 100) -> list[dict[str, Any]]:
        if identity["role"] != "admin":
            raise PermissionError("Administrator role required")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT audit_id, actor, action, resource_type, resource_id, outcome, "
                    "correlation_id, metadata_json, occurred_at FROM audit_events "
                    "ORDER BY occurred_at DESC LIMIT %s",
                    (limit,),
                )
                return [{**row, "audit_id": str(row["audit_id"])} for row in cursor.fetchall()]
