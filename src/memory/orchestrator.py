"""Governed memory writes, retrieval and deletion on PostgreSQL."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from rag.retriever import Retriever
from rag.vector_store import VectorStore, _vector_literal
from storage.postgres import PostgresDatabase


class MemoryOrchestrator:
    """Read only approved, in-scope memory; never auto-approve a write."""

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
                    "status, source_event_id, valid_until) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector, 'candidate', %s, %s)",
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
                    ),
                )
        return memory_id

    def approve(self, memory_id: str, identity: dict[str, str]) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE memories SET status = 'approved' "
                    "WHERE memory_id = %s AND status = 'candidate'",
                    (memory_id,),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("Memory not found or cannot be approved")
                cursor.execute(
                    "INSERT INTO memory_acl "
                    "(memory_id, tenant_id, subject_type, subject_id, permission) "
                    "VALUES (%s, %s, 'user', %s, 'admin') ON CONFLICT DO NOTHING",
                    (memory_id, identity["tenant_id"], identity["username"]),
                )

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
                    "SELECT kind FROM memories WHERE memory_id = %s AND status <> 'deleted'",
                    (memory_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Memory not found")
                cursor.execute(
                    "UPDATE memories SET status = 'superseded' WHERE memory_id = %s",
                    (memory_id,),
                )
        replacement_id = self.create_candidate(identity, row["kind"], content, source_event_id)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
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
                    "SELECT memory_id, kind, content, source_event_id, "
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
                "metadata": {"source_event_id": str(row["source_event_id"]), "kind": row["kind"]},
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
