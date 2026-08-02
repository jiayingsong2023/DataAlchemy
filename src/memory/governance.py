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

    def revoke_sources(self, source_event_ids: list[str], identity: dict[str, str], policy_version: str) -> list[str]:
        """Retire approved memories that only depend on revoked conversation events."""
        if not source_event_ids:
            return []
        revoked: list[str] = []
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT m.memory_id, m.policy_version FROM memories m "
                    "JOIN memory_sources s ON s.memory_id = m.memory_id "
                    "WHERE s.conversation_event_id = ANY(%s) AND m.status = 'approved' "
                    "AND NOT EXISTS (SELECT 1 FROM memory_sources keep "
                    "WHERE keep.memory_id = m.memory_id AND keep.conversation_event_id IS NOT NULL "
                    "AND NOT (keep.conversation_event_id = ANY(%s))) "
                    "GROUP BY m.memory_id, m.policy_version",
                    (source_event_ids, source_event_ids),
                )
                for row in cursor.fetchall():
                    cursor.execute(
                        "UPDATE memories SET status = 'superseded', decision_reason = 'source_revoked', "
                        "row_version = row_version + 1 WHERE memory_id = %s AND status = 'approved'",
                        (row["memory_id"],),
                    )
                    cursor.execute(
                        "INSERT INTO memory_policy_events (policy_event_id, memory_id, tenant_id, policy_version, action, before_json, after_json, actor) "
                        "VALUES (%s, %s, %s, %s, 'source_revoked', %s::jsonb, %s::jsonb, %s)",
                        (str(uuid.uuid4()), row["memory_id"], identity["tenant_id"], policy_version, json.dumps({"status": "approved"}), json.dumps({"status": "superseded", "reason": "source_revoked"}), identity["username"]),
                    )
                    revoked.append(str(row["memory_id"]))
        return revoked

    def resolve_conflict(self, selected_memory_id: str, identity: dict[str, str], policy_version: str) -> None:
        if identity.get("role") != "admin":
            raise PermissionError("Administrator role required to resolve memory conflicts")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT scope_type, scope_id, kind, claim_key, status FROM memories WHERE memory_id = %s FOR UPDATE",
                    (selected_memory_id,),
                )
                selected = cursor.fetchone()
                if selected is None or selected["status"] != "conflicted":
                    raise PermissionError("Conflicted memory not found")
                cursor.execute(
                    "UPDATE memories SET status = 'superseded', decision_reason = 'conflict_resolved', row_version = row_version + 1 "
                    "WHERE tenant_id = %s AND scope_type = %s AND scope_id = %s AND kind = %s AND claim_key = %s "
                    "AND memory_id <> %s AND status IN ('approved', 'candidate', 'conflicted')",
                    (identity["tenant_id"], selected["scope_type"], selected["scope_id"], selected["kind"], selected["claim_key"], selected_memory_id),
                )
                cursor.execute(
                    "UPDATE memories SET status = 'approved', decision_reason = 'conflict_admin_resolved', decided_by = %s, decided_at = now(), row_version = row_version + 1 "
                    "WHERE memory_id = %s AND status = 'conflicted'",
                    (identity["username"], selected_memory_id),
                )
                cursor.execute(
                    "INSERT INTO memory_policy_events (policy_event_id, memory_id, tenant_id, policy_version, action, before_json, after_json, actor) "
                    "VALUES (%s, %s, %s, %s, 'conflict_resolved', %s::jsonb, %s::jsonb, %s)",
                    (str(uuid.uuid4()), selected_memory_id, identity["tenant_id"], policy_version, json.dumps({"status": "conflicted"}), json.dumps({"status": "approved"}), identity["username"]),
                )
