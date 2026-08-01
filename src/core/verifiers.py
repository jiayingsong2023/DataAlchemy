"""Deterministic, read-only verifier contracts for the agent harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from storage.postgres import PostgresDatabase


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
    handler: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], "ReadOnlyServices"], VerificationResult]
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
                    "SELECT d.document_id, d.source_uri, d.content_hash, d.version, d.status, "
                    "count(c.chunk_id) AS chunk_count "
                    "FROM documents d LEFT JOIN document_chunks c ON c.document_id = d.document_id "
                    "WHERE d.document_id = ANY(%s) GROUP BY d.document_id",
                    (document_ids,),
                )
                return [{**row, "document_id": str(row["document_id"])} for row in cursor.fetchall()]

    def matching_chunks(self, document_id: str, query: str) -> int:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS count FROM document_chunks "
                    "WHERE document_id = %s AND fts @@ plainto_tsquery('simple', %s)",
                    (document_id, query),
                )
                return int(cursor.fetchone()["count"])

    def memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, status, source_event_id, valid_until, content_hash FROM memories "
                    "WHERE memory_id = %s",
                    (memory_id,),
                )
                return cursor.fetchone()

    def release(self, release_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT release_id, status, manifest_json FROM release_records WHERE release_id = %s",
                    (release_id,),
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


def _ingest(
    criterion: dict[str, Any], _task: dict[str, Any], result: dict[str, Any], services: ReadOnlyServices
) -> VerificationResult:
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    documents = services.documents(document_ids)
    if len(documents) != len(document_ids):
        return VerificationResult("failed", {"document_count": len(documents)}, "document_missing")
    if any(item["status"] != "ready" or item["chunk_count"] < 1 for item in documents):
        return VerificationResult("failed", {"documents": document_ids}, "document_not_ready")
    artifact_hashes = {
        item["id"]: item["sha256"]
        for item in result.get("artifacts", [])
        if item.get("store") == "postgres" and item.get("kind") == "document"
    }
    if any(artifact_hashes.get(item["document_id"]) != item["content_hash"] for item in documents):
        return VerificationResult("failed", {}, "document_hash_mismatch")
    max_rejected = criterion["parameters"].get("max_rejected", 0)
    if result.get("metrics", {}).get("rejected", 0) > max_rejected:
        return VerificationResult("failed", {"rejected": result["metrics"]["rejected"]}, "rejected_limit")
    return VerificationResult("passed", {"document_count": len(documents)})


def _retrieval(
    criterion: dict[str, Any], _task: dict[str, Any], result: dict[str, Any], services: ReadOnlyServices
) -> VerificationResult:
    query = criterion["parameters"].get("query", "")
    document_ids = result.get("output", {}).get("document_ids", [])
    if not isinstance(query, str) or not query.strip() or not document_ids:
        return VerificationResult("failed", {}, "retrieval_parameters_missing")
    matches = sum(services.matching_chunks(document_id, query) for document_id in document_ids)
    if matches < 1:
        return VerificationResult("failed", {"matches": matches}, "retrieval_not_found")
    return VerificationResult("passed", {"matches": matches})


def _memory(
    criterion: dict[str, Any], _task: dict[str, Any], _result: dict[str, Any], services: ReadOnlyServices
) -> VerificationResult:
    memory_id = criterion["parameters"].get("memory_id")
    row = services.memory(memory_id) if isinstance(memory_id, str) else None
    if row is None or row["status"] != "approved":
        return VerificationResult("failed", {}, "memory_not_approved")
    return VerificationResult("passed", {"memory_id": str(row["memory_id"])})


def _release(
    criterion: dict[str, Any], _task: dict[str, Any], _result: dict[str, Any], services: ReadOnlyServices
) -> VerificationResult:
    release_id = criterion["parameters"].get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    manifest = row["manifest_json"] if row else {}
    if row is None or not manifest.get("evaluation", {}).get("passed") or not manifest.get("rollback_to"):
        return VerificationResult("failed", {}, "release_guardrail_missing")
    return VerificationResult("passed", {"release_id": str(row["release_id"])})


def default_verifiers() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(VerifierSpec("verify_ingest", 1, _ingest))
    registry.register(VerifierSpec("verify_retrieval", 1, _retrieval))
    registry.register(VerifierSpec("verify_memory", 1, _memory))
    registry.register(VerifierSpec("verify_release", 1, _release))
    return registry
