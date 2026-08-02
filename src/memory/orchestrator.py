"""Governed memory writes, retrieval and deletion on PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

from rag.retriever import Retriever
from rag.vector_store import VectorStore, _vector_literal
from storage.postgres import PostgresDatabase


class MemoryOrchestrator:
    """Read approved, in-scope memory and apply versioned H4 policy decisions."""

    def __init__(self, database_url: str, vector_store: VectorStore, retriever: Retriever):
        self.database = PostgresDatabase(database_url)
        self.vector_store = vector_store
        self.retriever = retriever

    def create_candidate(
        self,
        identity: dict[str, str],
        kind: str,
        content: str,
        source_event_id: str,
        valid_until: datetime | None = None,
    ) -> str:
        if kind not in {"episodic", "profile", "procedural"}:
            raise ValueError("Unsupported memory kind")
        if not content.strip():
            raise ValueError("Memory content cannot be empty")
        self.vector_store._load_model()
        assert self.vector_store.model is not None
        embedding = _vector_literal(
            self.vector_store.model.encode([content], convert_to_numpy=True)[0]
        )
        memory_id = str(uuid.uuid4())
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO memories "
                    "(memory_id, tenant_id, owner_id, kind, content, content_hash, embedding, "
                    "status, source_event_id, valid_until, scope_type, scope_id, claim_key, risk_class, trust_label) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, 'candidate', %s, %s, 'personal', %s, %s, 'legacy', 'trusted_user')",
                    (
                        memory_id,
                        identity["tenant_id"],
                        identity["username"],
                        kind,
                        content,
                        content_hash,
                        embedding,
                        source_event_id,
                        valid_until,
                        identity["username"],
                        f"legacy.{kind}.{content_hash[:16]}",
                    ),
                )
        return memory_id

    def create_governed_candidate(
        self, identity: dict[str, str], candidate: dict[str, Any], *, auto_memory_enabled: bool = False
    ) -> dict[str, Any]:
        """Persist a sourced candidate; policy, not a model, controls approval."""
        required = {"kind", "content", "scope_type", "scope_id", "claim_key", "source_event_ids"}
        if not required <= candidate.keys() or not candidate["source_event_ids"]:
            raise ValueError("Memory candidate needs kind, scope, claim key and source events")
        try:
            candidate["source_event_ids"] = [str(uuid.UUID(str(item))) for item in candidate["source_event_ids"]]
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("Memory source event id is invalid") from error
        content = str(candidate["content"]).strip()
        if not content or len(content) > 4000:
            raise ValueError("Memory candidate content is invalid")
        if candidate["scope_type"] not in {"personal", "team", "tenant"}:
            raise ValueError("Unsupported memory scope")
        if candidate["scope_type"] == "personal" and candidate["scope_id"] != identity["username"]:
            raise PermissionError("Personal memory owner mismatch")
        trust = candidate.get("trust_label", "legacy_unverified")
        risk = candidate.get("risk_class", "legacy")
        sensitivity = candidate.get("sensitivity_label", "unknown")
        if re.search(r"(?i)(password|passwd|secret|token|credential|api[_ -]?key|密码|密钥|凭据|口令)", content):
            sensitivity = "credential"
        if trust not in {"trusted_user", "trusted_system", "verified_tool", "untrusted_external", "legacy_unverified"}:
            raise ValueError("Unsupported trust label")
        if risk not in {"low", "shared", "high", "prohibited", "legacy"}:
            raise ValueError("Unsupported risk class")
        self.vector_store._load_model()
        assert self.vector_store.model is not None
        embedding = _vector_literal(
            self.vector_store.model.encode([content], convert_to_numpy=True)[0]
        )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        memory_id = str(uuid.uuid4())
        status = "candidate"
        decision_reason = "approval_required"
        if sensitivity in {"secret", "credential", "auth", "access_control", "health", "financial", "legal"}:
            status = "rejected"
            decision_reason = "prohibited_sensitive_category"
        elif trust in {"untrusted_external", "legacy_unverified"}:
            status = "rejected"
            decision_reason = "untrusted_source"
        elif (
            auto_memory_enabled
            and candidate["scope_type"] == "personal"
            and risk == "low"
            and float(candidate.get("confidence", 0)) >= 0.9
            and candidate["scope_id"] == identity["username"]
        ):
            status = "approved"
            decision_reason = "personal_low_risk_auto_memory"
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, content_sha256 FROM conversation_events WHERE event_id = ANY(%s)",
                    (candidate["source_event_ids"],),
                )
                visible_sources = {str(item["event_id"]): item["content_sha256"] for item in cursor.fetchall()}
                if set(visible_sources) != {str(item) for item in candidate["source_event_ids"]}:
                    raise PermissionError("Memory source is outside the caller scope")
                cursor.execute(
                    "SELECT memory_id, status FROM memories WHERE tenant_id = %s AND scope_type = %s "
                    "AND scope_id = %s AND kind = %s AND claim_key = %s AND status IN ('approved', 'candidate', 'conflicted') "
                    "AND content_hash = %s",
                    (identity["tenant_id"], candidate["scope_type"], candidate["scope_id"], candidate["kind"], candidate["claim_key"], content_hash),
                )
                duplicate = cursor.fetchone()
                if duplicate:
                    return {"memory_id": str(duplicate["memory_id"]), "status": duplicate["status"], "deduplicated": True}
                cursor.execute(
                    "SELECT memory_id FROM memories WHERE tenant_id = %s AND scope_type = %s AND scope_id = %s "
                    "AND kind = %s AND claim_key = %s AND status IN ('approved', 'candidate', 'conflicted') "
                    "AND content_hash <> %s LIMIT 1",
                    (identity["tenant_id"], candidate["scope_type"], candidate["scope_id"], candidate["kind"], candidate["claim_key"], content_hash),
                )
                if cursor.fetchone() is not None:
                    status = "conflicted"
                    decision_reason = "claim_value_conflict"
                cursor.execute(
                    "INSERT INTO memories (memory_id, tenant_id, owner_id, kind, content, content_hash, embedding, status, "
                    "source_event_id, valid_until, scope_type, scope_id, claim_key, confidence, trust_label, sensitivity_label, "
                    "risk_class, policy_version, decision_reason, decided_by, decided_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                    (
                        memory_id,
                        identity["tenant_id"],
                        identity["username"],
                        candidate["kind"],
                        content,
                        content_hash,
                        embedding,
                        status,
                        candidate.get("valid_until"),
                        candidate["scope_type"],
                        candidate["scope_id"],
                        candidate["claim_key"],
                        candidate.get("confidence"),
                        trust,
                        sensitivity,
                        risk,
                        candidate.get("policy_version", "memory-policy.v1"),
                        decision_reason,
                        identity["username"] if status == "approved" else None,
                    ),
                )
                for source_id in candidate["source_event_ids"]:
                    cursor.execute(
                        "INSERT INTO memory_sources (memory_id, tenant_id, conversation_event_id, source_type, source_sha256) "
                        "VALUES (%s, %s, %s, 'conversation', %s) ON CONFLICT DO NOTHING",
                        (memory_id, identity["tenant_id"], source_id, visible_sources[str(source_id)]),
                    )
                cursor.execute(
                    "INSERT INTO memory_policy_events (policy_event_id, memory_id, tenant_id, policy_version, action, before_json, after_json, actor) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
                    (
                        str(uuid.uuid4()), memory_id, identity["tenant_id"], candidate.get("policy_version", "memory-policy.v1"),
                        "auto_approved" if status == "approved" else "rejected" if status == "rejected" else "conflict_detected" if status == "conflicted" else "candidate_created",
                        json.dumps({"status": "new"}), json.dumps({"status": status, "reason": decision_reason}), identity["username"],
                    ),
                )
                if status == "approved":
                    cursor.execute(
                        "INSERT INTO memory_acl (memory_id, tenant_id, subject_type, subject_id, permission) "
                        "VALUES (%s, %s, 'user', %s, 'read') ON CONFLICT DO NOTHING",
                        (memory_id, identity["tenant_id"], identity["username"]),
                    )
        return {"memory_id": memory_id, "status": status, "reason": decision_reason, "deduplicated": False}

    def approve(self, memory_id: str, identity: dict[str, str]) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT owner_id, scope_type, risk_class, status, row_version FROM memories "
                    "WHERE memory_id = %s FOR UPDATE",
                    (memory_id,),
                )
                row = cursor.fetchone()
                if row is None or row["status"] != "candidate":
                    raise PermissionError("Memory not found or cannot be approved")
                if row["scope_type"] == "personal" and row["owner_id"] != identity["username"] and identity.get("role") != "admin":
                    raise PermissionError("Memory approval is outside the caller scope")
                if row["scope_type"] != "personal" and identity.get("role") != "admin":
                    raise PermissionError("Administrator role required to approve shared memory")
                if row["scope_type"] != "personal" and row["owner_id"] == identity["username"]:
                    raise PermissionError("A shared memory requires a different approver")
                cursor.execute(
                    "SELECT supersedes_memory_id FROM memory_versions WHERE memory_id = %s",
                    (memory_id,),
                )
                supersedes = cursor.fetchone()
                if supersedes and supersedes["supersedes_memory_id"]:
                    cursor.execute(
                        "UPDATE memories SET status = 'superseded', row_version = row_version + 1 "
                        "WHERE memory_id = %s AND status = 'approved'",
                        (supersedes["supersedes_memory_id"],),
                    )
                cursor.execute(
                    "UPDATE memories SET status = 'approved', decision_reason = 'admin_approved', "
                    "decided_by = %s, decided_at = now(), row_version = row_version + 1 "
                    "WHERE memory_id = %s AND row_version = %s",
                    (identity["username"], memory_id, row["row_version"]),
                )
                cursor.execute(
                    "INSERT INTO memory_acl "
                    "(memory_id, tenant_id, subject_type, subject_id, permission) "
                    "SELECT %s, tenant_id, CASE scope_type WHEN 'tenant' THEN 'tenant' WHEN 'team' THEN 'role' ELSE 'user' END, "
                    "COALESCE(scope_id, owner_id), 'read' FROM memories WHERE memory_id = %s "
                    "ON CONFLICT DO NOTHING",
                    (memory_id, memory_id),
                )
                if row["risk_class"] != "legacy":
                    cursor.execute(
                        "INSERT INTO memory_policy_events (policy_event_id, memory_id, tenant_id, policy_version, action, before_json, after_json, actor) "
                        "SELECT %s, memory_id, tenant_id, policy_version, 'approved', %s::jsonb, %s::jsonb, %s "
                        "FROM memories WHERE memory_id = %s",
                        (
                            str(uuid.uuid4()),
                            json.dumps({"status": "candidate"}),
                            json.dumps({"status": "approved"}),
                            identity["username"],
                            memory_id,
                        ),
                    )

    def reject(self, memory_id: str, identity: dict[str, str]) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tenant_id, owner_id, policy_version, status, row_version FROM memories "
                    "WHERE memory_id = %s FOR UPDATE",
                    (memory_id,),
                )
                row = cursor.fetchone()
                if row is None or row["status"] != "candidate":
                    raise PermissionError("Memory not found or cannot be rejected")
                if row["owner_id"] != identity["username"] and identity.get("role") != "admin":
                    raise PermissionError("Memory rejection is outside the caller scope")
                cursor.execute(
                    "UPDATE memories SET status = 'rejected', decision_reason = 'rejected_by_user', "
                    "decided_by = %s, decided_at = now(), row_version = row_version + 1 "
                    "WHERE memory_id = %s AND row_version = %s",
                    (identity["username"], memory_id, row["row_version"]),
                )
                cursor.execute(
                    "INSERT INTO memory_policy_events (policy_event_id, memory_id, tenant_id, policy_version, action, before_json, after_json, actor) "
                    "VALUES (%s, %s, %s, %s, 'rejected', %s::jsonb, %s::jsonb, %s)",
                    (str(uuid.uuid4()), memory_id, row["tenant_id"], row["policy_version"], json.dumps({"status": "candidate"}), json.dumps({"status": "rejected"}), identity["username"]),
                )

    def list(self, identity: dict[str, str], *, include_candidates: bool = True) -> list[dict[str, Any]]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, kind, content, status, scope_type, scope_id, claim_key, confidence, "
                    "trust_label, sensitivity_label, risk_class, valid_until, decision_reason, decided_by, row_version "
                    "FROM memories WHERE tenant_id = %s AND (owner_id = %s OR %s = 'admin' OR status = 'approved') "
                    "ORDER BY created_at DESC LIMIT 200",
                    (identity["tenant_id"], identity["username"], identity["role"]),
                )
                rows = cursor.fetchall()
        if not include_candidates:
            rows = [row for row in rows if row["status"] == "approved"]
        return [{**row, "memory_id": str(row["memory_id"])} for row in rows]

    def revise(
        self,
        memory_id: str,
        content: str,
        source_event_id: str,
        identity: dict[str, str],
    ) -> str:
        """Supersede a memory; the replacement still needs approval."""
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT kind, content, scope_type, scope_id, claim_key, confidence, trust_label, sensitivity_label, risk_class, policy_version "
                    "FROM memories WHERE memory_id = %s AND status <> 'deleted'",
                    (memory_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Memory not found")
        if row["risk_class"] != "legacy":
            replacement = self.create_governed_candidate(
                identity,
                {
                    "kind": row["kind"],
                    "content": content,
                    "scope_type": row["scope_type"],
                    "scope_id": row["scope_id"],
                    "claim_key": row["claim_key"],
                    "source_event_ids": [source_event_id],
                    "confidence": row["confidence"],
                    "trust_label": row["trust_label"],
                    "sensitivity_label": row["sensitivity_label"],
                    "risk_class": row["risk_class"],
                    "policy_version": row["policy_version"],
                }
            )
            replacement_id = replacement["memory_id"]
        else:
            replacement_id = self.create_candidate(identity, row["kind"], content, source_event_id)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                if row["risk_class"] == "legacy":
                    cursor.execute(
                        "UPDATE memories SET status = 'superseded' WHERE memory_id = %s AND status = 'approved'",
                        (memory_id,),
                    )
                cursor.execute(
                    "INSERT INTO memory_versions "
                    "(memory_id, supersedes_memory_id, decision_event_id) VALUES (%s, %s, %s)",
                    (replacement_id, memory_id, source_event_id),
                )
        return replacement_id

    def retrieve(
        self, query: str, identity: dict[str, str], top_k: int = 8
    ) -> list[dict[str, Any]]:
        self.vector_store._load_model()
        assert self.vector_store.model is not None
        embedding = _vector_literal(
            self.vector_store.model.encode([query], convert_to_numpy=True)[0]
        )
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, kind, content, source_event_id, scope_type, scope_id, claim_key, "
                    "confidence, trust_label, risk_class, valid_until, "
                    "ARRAY(SELECT conversation_event_id::text FROM memory_sources s WHERE s.memory_id = memories.memory_id) AS source_event_ids, "
                    "1 - (embedding <=> %s::vector) AS score FROM memories "
                    "WHERE status = 'approved' "
                    "AND (valid_until IS NULL OR valid_until > now()) "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (embedding, embedding, top_k),
                )
                rows = cursor.fetchall()
        return [
            {
                "memory_id": str(row["memory_id"]),
                "memory_kind": row["kind"],
                "text": row["content"],
                "source_event_id": str(row["source_event_id"]),
                "metadata": {
                    "source_event_id": str(row["source_event_id"]),
                    "source_event_ids": row["source_event_ids"] or [],
                    "kind": row["kind"],
                },
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "claim_key": row["claim_key"],
                "trust_label": row["trust_label"],
                "risk_class": row["risk_class"],
                "score": float(row["score"]),
                "method": "memory",
            }
            for row in rows
        ]

    def context(self, query: str, identity: dict[str, str]) -> list[dict[str, Any]]:
        """Return bounded, source-labelled documents and approved memories."""
        documents = self.retriever.retrieve(query, identity, top_k=8)
        memories = self.retrieve(query, identity, top_k=4)
        return [{**item, "context_type": "document"} for item in documents] + [
            {**item, "context_type": "memory"} for item in memories
        ]

    def delete(self, target_type: str, target_id: str, identity: dict[str, str]) -> str:
        if target_type not in {"document", "memory"}:
            raise ValueError("target_type must be document or memory")
        request_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO deletion_requests "
                    "(request_id, tenant_id, target_type, target_id, requested_by, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'pending')",
                    (
                        request_id,
                        identity["tenant_id"],
                        target_type,
                        target_id,
                        identity["username"],
                    ),
                )
                if target_type == "document":
                    cursor.execute(
                        "UPDATE documents SET status = 'deleted', deleted_at = now() "
                        "WHERE document_id = %s",
                        (target_id,),
                    )
                    if cursor.rowcount != 1:
                        raise PermissionError("Document not found")
                    cursor.execute(
                        "DELETE FROM document_chunks WHERE document_id = %s", (target_id,)
                    )
                else:
                    cursor.execute(
                        "UPDATE memories SET status = 'deleted', embedding = NULL, "
                        "deleted_at = now() "
                        "WHERE memory_id = %s",
                        (target_id,),
                    )
                    if cursor.rowcount != 1:
                        raise PermissionError("Memory not found")
                cursor.execute(
                    "UPDATE deletion_requests SET status = 'completed', completed_at = now() "
                    "WHERE request_id = %s",
                    (request_id,),
                )
        return request_id
