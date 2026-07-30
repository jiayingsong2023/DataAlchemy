"""Approval and rollback state machine for model, prompt and tool releases."""

from __future__ import annotations

import json
import uuid
from typing import Any

from storage.audit import AuditLog
from storage.postgres import PostgresDatabase


class ReleaseGovernance:
    transitions = {
        "candidate": {"shadow", "rejected"},
        "shadow": {"canary", "rejected"},
        "canary": {"promoted", "rolled_back"},
        "promoted": {"rolled_back"},
        "rejected": set(),
        "rolled_back": set(),
    }

    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)

    def create_candidate(self, identity: dict[str, str], manifest: dict[str, Any]) -> str:
        self._admin(identity)
        required = {"code_version", "evaluation", "rollback_to"}
        if missing := required - manifest.keys():
            raise ValueError(f"Release manifest missing: {', '.join(sorted(missing))}")
        if manifest["evaluation"].get("passed") is not True:
            raise ValueError("Release evaluation must pass before shadowing")
        release_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO release_records (release_id, tenant_id, status, manifest_json) "
                    "VALUES (%s, %s, 'candidate', %s::jsonb)",
                    (release_id, identity["tenant_id"], json.dumps(manifest, ensure_ascii=False)),
                )
        self.audit.record(identity, "release.candidate", "release", resource_id=release_id)
        return release_id

    def advance(self, release_id: str, target: str, identity: dict[str, str]) -> dict[str, Any]:
        self._admin(identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM release_records WHERE release_id = %s FOR UPDATE", (release_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Release not found")
                if target not in self.transitions[row["status"]]:
                    raise ValueError(f"Invalid release transition: {row['status']} → {target}")
                cursor.execute(
                    "UPDATE release_records SET status = %s, approved_by = %s, updated_at = now() "
                    "WHERE release_id = %s",
                    (target, identity["username"], release_id),
                )
                row["status"] = target
                row["approved_by"] = identity["username"]
        self.audit.record(
            identity,
            f"release.{target}",
            "release",
            resource_id=release_id,
            correlation_id=release_id,
        )
        return {**row, "release_id": str(row["release_id"])}

    def observe(self, release_id: str, metrics: dict[str, float], identity: dict[str, str]) -> str:
        """Promote only a healthy canary; breach configured error/latency limits rolls it back."""
        self._admin(identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM release_records WHERE release_id = %s", (release_id,))
                row = cursor.fetchone()
        if row is None or row["status"] != "canary":
            raise PermissionError("Canary release not found")
        guardrails = row["manifest_json"].get("guardrails", {})
        breached = (
            metrics.get("error_rate", 0.0) > guardrails.get("max_error_rate", 1.0)
            or metrics.get("p95_ms", 0.0) > guardrails.get("max_p95_ms", float("inf"))
        )
        target = "rolled_back" if breached else "promoted"
        return self.advance(release_id, target, identity)["status"]

    @staticmethod
    def _admin(identity: dict[str, str]) -> None:
        if identity["role"] != "admin":
            raise PermissionError("Administrator role required")
