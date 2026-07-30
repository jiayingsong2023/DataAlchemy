"""PostgreSQL + pgvector document store used by the Phase 2 retriever."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

from config import DATABASE_URL, get_model_config
from rag.chunkers.base import Chunker
from storage.postgres import PostgresDatabase


def _vector_literal(vector: Any) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def _load_sentence_transformer(model_name: str, **kwargs: Any) -> Any:
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, **kwargs)


class VectorStore:
    """Document repository backed exclusively by PostgreSQL + pgvector."""

    def __init__(self, model_name: str | None = None, database_url: str | None = None):
        model_b = get_model_config("model_b")
        self.model_name = model_name or model_b.get("model_id", "BAAI/bge-small-zh-v1.5")
        self.database = PostgresDatabase(database_url or DATABASE_URL)
        self.model: Any = None

    def _load_model(self) -> None:
        if self.model is not None:
            return
        model_b = get_model_config("model_b")
        local_path = model_b.get("model_path")
        offline = os.getenv("TRANSFORMERS_OFFLINE") == "1"
        if (local_path and os.path.exists(local_path)) or offline:
            path = local_path if local_path and os.path.exists(local_path) else self.model_name
            self.model = _load_sentence_transformer(path, local_files_only=True)
        else:
            self.model = _load_sentence_transformer(self.model_name)

    @staticmethod
    def _identity(identity: dict[str, str] | None) -> dict[str, str]:
        if identity is None:
            raise ValueError("identity is required for document persistence")
        return identity

    def add_documents(
        self,
        documents: list[dict[str, Any]],
        identity: dict[str, str] | None,
        chunker: Chunker | None = None,
    ) -> list[str]:
        """Store documents and chunks atomically; duplicate content is a no-op."""
        identity = self._identity(identity)
        prepared = self._prepare_documents(documents, chunker)
        if not prepared:
            return []
        self._load_model()
        assert self.model is not None
        embeddings = iter(
            self.model.encode(
                [chunk["text"] for item in prepared for chunk in item["chunks"]],
                convert_to_numpy=True,
            )
        )
        stored: list[str] = []
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                for document in prepared:
                    chunk_embeddings = [next(embeddings) for _ in document["chunks"]]
                    source_uri = document["source"]
                    content_hash = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
                    cursor.execute(
                        "SELECT document_id FROM documents WHERE tenant_id = %s "
                        "AND source_uri = %s AND content_hash = %s",
                        (identity["tenant_id"], source_uri, content_hash),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        stored.append(str(existing["document_id"]))
                        continue
                    document_id = uuid.uuid4()
                    cursor.execute(
                        "INSERT INTO documents "
                        "(document_id, tenant_id, owner_id, source_uri, content_hash, "
                        "status, metadata_json) "
                        "VALUES (%s, %s, %s, %s, %s, 'building', %s::jsonb)",
                        (
                            document_id,
                            identity["tenant_id"],
                            identity["username"],
                            source_uri,
                            content_hash,
                            json.dumps(document["metadata"], ensure_ascii=False),
                        ),
                    )
                    cursor.execute(
                        "INSERT INTO document_acl "
                        "(document_id, tenant_id, subject_type, subject_id, permission) "
                        "VALUES (%s, %s, 'user', %s, 'admin')",
                        (document_id, identity["tenant_id"], identity["username"]),
                    )
                    acl = document["metadata"].get("acl", [("tenant", identity["tenant_id"])])
                    for subject_type, subject_id in acl:
                        cursor.execute(
                            "INSERT INTO document_acl "
                            "(document_id, tenant_id, subject_type, subject_id, permission) "
                            "VALUES (%s, %s, %s, %s, 'read') ON CONFLICT DO NOTHING",
                            (document_id, identity["tenant_id"], subject_type, subject_id),
                        )
                    for ordinal, (chunk, embedding) in enumerate(
                        zip(document["chunks"], chunk_embeddings, strict=True)
                    ):
                        lexemes = " ".join(__import__("jieba").cut(chunk["text"]))
                        cursor.execute(
                            "INSERT INTO document_chunks "
                            "(chunk_id, document_id, ordinal, text, lexemes, fts, embedding, "
                            "metadata_json) "
                            "VALUES (%s, %s, %s, %s, %s, "
                            "to_tsvector('simple', %s), %s::vector, %s::jsonb)",
                            (
                                uuid.uuid4(),
                                document_id,
                                ordinal,
                                chunk["text"],
                                lexemes,
                                lexemes,
                                _vector_literal(embedding),
                                json.dumps(chunk["metadata"], ensure_ascii=False),
                            ),
                        )
                    cursor.execute(
                        "UPDATE documents SET status = 'ready' WHERE document_id = %s",
                        (document_id,),
                    )
                    stored.append(str(document_id))
        return stored

    def replace_acl(
        self, document_ids: list[str], acl: list[tuple[str, str]], identity: dict[str, str]
    ) -> None:
        """Make a connector's readable-subject snapshot authoritative."""
        if not document_ids:
            return
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM document_acl WHERE document_id = ANY(%s) "
                    "AND NOT (subject_type = 'user' AND subject_id = %s AND permission = 'admin')",
                    (document_ids, identity["username"]),
                )
                for document_id in document_ids:
                    for subject_type, subject_id in acl:
                        cursor.execute(
                            "INSERT INTO document_acl "
                            "(document_id, tenant_id, subject_type, subject_id, permission) "
                            "VALUES (%s, %s, %s, %s, 'read') ON CONFLICT DO NOTHING",
                            (document_id, identity["tenant_id"], subject_type, subject_id),
                        )

    def _prepare_documents(
        self, documents: list[dict[str, Any]], chunker: Chunker | None
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for document in documents:
            metadata = document.get("metadata", {}).copy()
            source = document.get("source") or metadata.get("source") or "unknown"
            chunks = (
                [{"text": document["text"], "metadata": metadata}]
                if chunker is None
                else chunker.split(document["text"], metadata=metadata)
            )
            clean_chunks = [
                {"text": chunk["text"].strip(), "metadata": chunk.get("metadata", metadata)}
                for chunk in chunks
                if chunk["text"].strip()
            ]
            if clean_chunks:
                prepared.append(
                    {
                        "text": document["text"],
                        "source": source,
                        "metadata": metadata,
                        "chunks": clean_chunks,
                    }
                )
        return prepared

    def search_vector(
        self, query: str, identity: dict[str, str], top_k: int = 20
    ) -> list[dict[str, Any]]:
        self._load_model()
        assert self.model is not None
        embedding = _vector_literal(self.model.encode([query], convert_to_numpy=True)[0])
        return self._search(
            identity,
            "SELECT c.chunk_id, c.text, d.source_uri, d.version, c.metadata_json, "
            "1 - (c.embedding <=> %s::vector) AS score FROM document_chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            "WHERE d.status = 'ready' "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s",
            (embedding, embedding, top_k),
            "vector",
        )

    def search_text(
        self, query: str, identity: dict[str, str], top_k: int = 20
    ) -> list[dict[str, Any]]:
        tokens = " ".join(__import__("jieba").cut(query))
        return self._search(
            identity,
            "SELECT c.chunk_id, c.text, d.source_uri, d.version, c.metadata_json, "
            "ts_rank_cd(c.fts, plainto_tsquery('simple', %s)) AS score FROM document_chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            "WHERE d.status = 'ready' AND c.fts @@ plainto_tsquery('simple', %s) "
            "ORDER BY score DESC LIMIT %s",
            (tokens, tokens, top_k),
            "text",
        )

    def _search(
        self, identity: dict[str, str], query: str, values: tuple[Any, ...], method: str
    ) -> list[dict[str, Any]]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, values)
                rows = cursor.fetchall()
        return [
            {
                "chunk_id": str(row["chunk_id"]),
                "text": row["text"],
                "source": row["source_uri"],
                "document_version": row["version"],
                "metadata": row["metadata_json"],
                "score": float(row["score"]),
                "method": method,
            }
            for row in rows
        ]
