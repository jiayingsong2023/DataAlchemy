"""Replayable, reversible lifecycle policies for approved memories."""

from __future__ import annotations

import json
import uuid
from typing import Any

from storage.audit import AuditLog
from storage.postgres import PostgresDatabase


class MemoryGovernance:
    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)

    def expire_due(self, identity: dict[str, str], policy_version: str) -> list[str]:
        """Retire approved, expired memories and preserve a policy decision record."""
        expired: list[str] = []
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, status, valid_until FROM memories "
                    "WHERE status = 'approved' AND valid_until <= now() FOR UPDATE"
                )
                for row in cursor.fetchall():
                    before = {"status": row["status"], "valid_until": str(row["valid_until"])}
                    cursor.execute(
                        "UPDATE memories SET status = 'superseded' WHERE memory_id = %s",
                        (row["memory_id"],),
                    )
                    cursor.execute(
                        "INSERT INTO memory_policy_events "
                        "(policy_event_id, memory_id, tenant_id, policy_version, action, "
                        "before_json, after_json, actor) VALUES (%s, %s, %s, %s, 'expired', "
                        "%s::jsonb, %s::jsonb, %s)",
                        (
                            str(uuid.uuid4()),
                            row["memory_id"],
                            identity["tenant_id"],
                            policy_version,
                            json.dumps(before),
                            json.dumps({"status": "superseded"}),
                            identity["username"],
                        ),
                    )
                    expired.append(str(row["memory_id"]))
        for memory_id in expired:
            self.audit.record(
                identity,
                "memory.expired",
                "memory",
                resource_id=memory_id,
                metadata={"policy_version": policy_version},
            )
        return expired

    def revert_expiry(self, policy_event_id: str, identity: dict[str, str]) -> None:
        """Restore an expired memory only when its recorded pre-state was approved."""
        event = self.replay(policy_event_id, identity)
        if event["action"] != "expired" or event["before_json"].get("status") != "approved":
            raise ValueError("Only approved expiry decisions can be reverted")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE memories SET status = 'approved', valid_until = NULL "
                    "WHERE memory_id = %s AND status = 'superseded'",
                    (event["memory_id"],),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("Memory cannot be restored")
                cursor.execute(
                    "INSERT INTO memory_policy_events "
                    "(policy_event_id, memory_id, tenant_id, policy_version, action, before_json, "
                    "after_json, actor) VALUES (%s, %s, %s, %s, 'reverted', %s::jsonb, "
                    "%s::jsonb, %s)",
                    (
                        str(uuid.uuid4()),
                        event["memory_id"],
                        identity["tenant_id"],
                        event["policy_version"],
                        json.dumps({"status": "superseded"}),
                        json.dumps({"status": "approved"}),
                        identity["username"],
                    ),
                )
        self.audit.record(
            identity, "memory.expiry_reverted", "memory", resource_id=event["memory_id"]
        )

    def replay(self, policy_event_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM memory_policy_events WHERE policy_event_id = %s",
                    (policy_event_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise PermissionError("Policy event not found")
        return {
            **row,
            "policy_event_id": str(row["policy_event_id"]),
            "memory_id": str(row["memory_id"]),
        }
