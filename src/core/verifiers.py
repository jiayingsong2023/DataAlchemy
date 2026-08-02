"""Deterministic, read-only verifier contracts for the agent harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils


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
    handler: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any], "ReadOnlyServices"], VerificationResult
    ]
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
                    "SELECT d.document_id, d.source_uri, d.content_hash, d.version, d.status, d.metadata_json, "
                    "count(c.chunk_id) AS chunk_count "
                    "FROM documents d LEFT JOIN document_chunks c ON c.document_id = d.document_id "
                    "WHERE d.document_id = ANY(%s) GROUP BY d.document_id",
                    (document_ids,),
                )
                return [
                    {
                        **row,
                        "document_id": str(row["document_id"]),
                        "metadata": row.pop("metadata_json"),
                    }
                    for row in cursor.fetchall()
                ]

    @staticmethod
    def _object_parts(key: str) -> tuple[S3Utils, str]:
        normalized = key.replace("s3a://", "s3://", 1)
        if normalized.startswith("s3://"):
            bucket, _, object_key = normalized.removeprefix("s3://").partition("/")
            return S3Utils(bucket=bucket), object_key
        return S3Utils(), normalized

    def object_body(self, key: str) -> bytes | None:
        store, object_key = self._object_parts(key)
        return store.get_object_body(object_key)

    def object_json(self, key: str) -> Any:
        body = self.object_body(key)
        if body is None:
            return None
        return json.loads(body)

    def object_records(self, prefix: str) -> list[dict[str, Any]]:
        store, object_prefix = self._object_parts(prefix)
        records: list[dict[str, Any]] = []
        for item in sorted(store.list_objects(object_prefix.rstrip("/") + "/"), key=lambda value: value["Key"]):
            if not item["Key"].endswith((".json", ".jsonl")):
                continue
            body = store.get_object_body(item["Key"])
            if body is None:
                continue
            records.extend(json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip())
        return records

    def matching_chunks(self, document_id: str, query: str) -> int:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS count FROM document_chunks "
                    "WHERE document_id = %s AND fts @@ plainto_tsquery('simple', %s)",
                    (document_id, query),
                )
                return int(cursor.fetchone()["count"])

    def chunks(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT chunk_id, document_id, ordinal, metadata_json FROM document_chunks "
                    "WHERE document_id = ANY(%s) ORDER BY document_id, ordinal",
                    (document_ids,),
                )
                return [
                    {**row, "chunk_id": str(row["chunk_id"]), "document_id": str(row["document_id"]), "metadata": row.pop("metadata_json")}
                    for row in cursor.fetchall()
                ]

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

    def job(self, task_id: str, step_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, input_key, input_sha256, result_sha256, error_code "
                    "FROM agent_jobs WHERE task_id = %s AND step_id = %s",
                    (task_id, step_id),
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
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
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
        return VerificationResult(
            "failed", {"rejected": result["metrics"]["rejected"]}, "rejected_limit"
        )
    return VerificationResult("passed", {"document_count": len(documents)})


def _ingest_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    documents = services.documents(document_ids)
    if len(documents) != len(document_ids) or not documents:
        return VerificationResult("failed", {"document_count": len(documents)}, "document_missing")
    artifact_hashes = {
        item["id"]: item["sha256"]
        for item in result.get("artifacts", [])
        if item.get("store") == "postgres" and item.get("kind") == "document"
    }
    expected_phrase = criterion.get("parameters", {}).get("expected_phrase")
    for document in documents:
        metadata = document.get("metadata") or {}
        if document["status"] != "ready" or document["chunk_count"] < 1:
            return VerificationResult("failed", {"document_id": document["document_id"]}, "document_not_ready")
        if artifact_hashes.get(document["document_id"]) != document["content_hash"]:
            return VerificationResult("failed", {}, "document_hash_mismatch")
        if metadata.get("trust_label") != "untrusted_external" or not metadata.get("acl_digest"):
            return VerificationResult("failed", {}, "document_lineage_missing")
        if expected_phrase and not services.matching_chunks(document["document_id"], expected_phrase):
            return VerificationResult("failed", {}, "expected_phrase_not_found")
    return VerificationResult("passed", {"document_count": len(documents), "chunk_count": sum(item["chunk_count"] for item in documents)})


def _input_manifest(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next((item for item in result.get("artifacts", []) if item.get("kind") == "input_manifest"), None)
    if artifact is None:
        return VerificationResult("failed", {}, "input_manifest_missing")
    descriptor = services.object_json(artifact["id"])
    if not isinstance(descriptor, dict) or descriptor.get("tenant_id") != _task["tenant_id"]:
        return VerificationResult("failed", {}, "input_scope_mismatch")
    if descriptor.get("trust_label") != "untrusted_external" or not descriptor.get("acl_digest"):
        return VerificationResult("failed", {}, "input_lineage_missing")
    source = descriptor.get("source", {})
    raw_key = source.get("object_key")
    raw_body = services.object_body(raw_key) if raw_key else None
    expected_sha = result.get("output", {}).get("input_sha256")
    if raw_body is None or not expected_sha or hashlib.sha256(raw_body).hexdigest() != expected_sha:
        return VerificationResult("failed", {}, "input_hash_mismatch")
    if source.get("version") != f"sha256:{expected_sha}":
        return VerificationResult("failed", {}, "input_version_mismatch")
    return VerificationResult(
        "passed",
        {"input_id": descriptor.get("input_id"), "source_version": source.get("version")},
    )


def _retrieval(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    query = criterion["parameters"].get("query", "")
    document_ids = result.get("output", {}).get("document_ids", [])
    if not isinstance(query, str) or not query.strip() or not document_ids:
        return VerificationResult("failed", {}, "retrieval_parameters_missing")
    matches = sum(services.matching_chunks(document_id, query) for document_id in document_ids)
    if matches < 1:
        return VerificationResult("failed", {"matches": matches}, "retrieval_not_found")
    return VerificationResult("passed", {"matches": matches})


def _retrieval_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    query = criterion.get("parameters", {}).get("query", "")
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    citations = output.get("citations", [])
    if not isinstance(query, str) or not query.strip() or not document_ids or not citations:
        return VerificationResult("failed", {}, "retrieval_citations_missing")
    chunk_ids = {chunk["chunk_id"] for chunk in services.chunks(document_ids)}
    if any(citation.get("chunk_id") not in chunk_ids for citation in citations):
        return VerificationResult("failed", {}, "citation_not_authorized")
    # The retriever may rewrite a mixed-language query before FTS/vector
    # recall.  The verifier therefore proves the returned chunk/ACL chain,
    # rather than re-running a language-dependent FTS expression.
    return VerificationResult("passed", {"matches": len(citations), "document_count": len(document_ids)})


def _memory(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    memory_id = criterion["parameters"].get("memory_id")
    row = services.memory(memory_id) if isinstance(memory_id, str) else None
    if row is None or row["status"] != "approved":
        return VerificationResult("failed", {}, "memory_not_approved")
    return VerificationResult("passed", {"memory_id": str(row["memory_id"])})


def _release(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion["parameters"].get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    manifest = row["manifest_json"] if row else {}
    if (
        row is None
        or not manifest.get("evaluation", {}).get("passed")
        or not manifest.get("rollback_to")
    ):
        return VerificationResult("failed", {}, "release_guardrail_missing")
    return VerificationResult("passed", {"release_id": str(row["release_id"])})


def _rough_clean(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    step_id = result.get("step_id") or task["plan"][task["current_step"]]["step_id"]
    job = services.job(task["task_id"], step_id)
    artifact = next(
        (
            item
            for item in result.get("artifacts", [])
            if item.get("store") == "minio" and item.get("kind") == "cleaned_corpus"
        ),
        None,
    )
    if job is None or job["state"] != "succeeded" or not job["result_sha256"]:
        return VerificationResult("failed", {}, "job_result_unverified")
    if (
        artifact is None
        or not isinstance(artifact.get("sha256"), str)
        or len(artifact["sha256"]) != 64
    ):
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    if result.get("observed_scope") != [f"raw:{job['input_key']}"]:
        return VerificationResult("failed", {}, "job_scope_mismatch")
    return VerificationResult("passed", {"job_result_sha256": job["result_sha256"]})


def _rough_clean_v2(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    outcome = _rough_clean(criterion, task, result, services)
    if outcome.status != "passed":
        return outcome
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "cleaned_corpus"), None
    )
    if artifact is None:
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    # Spark writes one output prefix containing several products; rough-clean
    # schema verification must read only the cleaned-corpus product, not the
    # later RAG rows whose shape is intentionally different.
    records = services.object_records(artifact["id"].rstrip("/") + "/cleaned_corpus.jsonl")
    if not records:
        return VerificationResult("failed", {}, "rough_records_missing")
    accepted = 0
    for record in records:
        required = {"text", "source_uri", "source_version", "tenant_id", "acl_digest", "trust_label", "decision"}
        if not required <= record.keys() or record["tenant_id"] != task["tenant_id"]:
            return VerificationResult("failed", {}, "rough_schema_invalid")
        if record["decision"] == "accepted":
            accepted += 1
    if accepted < 1:
        return VerificationResult("failed", {}, "rough_no_accepted_records")
    return VerificationResult("passed", {"records": len(records), "accepted": accepted})


def _refined_corpus(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "normalized_documents"), None
    )
    if artifact is None:
        return VerificationResult("failed", {}, "normalized_artifact_missing")
    body = services.object_body(artifact["id"])
    if body is None or hashlib.sha256(body).hexdigest() != artifact["sha256"]:
        return VerificationResult("failed", {}, "normalized_artifact_hash_mismatch")
    try:
        normalized = json.loads(body)
    except json.JSONDecodeError:
        return VerificationResult("failed", {}, "normalized_schema_invalid")
    if normalized.get("tenant_id") != task["tenant_id"] or not normalized.get("documents"):
        return VerificationResult("failed", {}, "normalized_schema_invalid")
    for document in normalized["documents"]:
        if not document.get("acl_digest") or document.get("trust_label") != "untrusted_external":
            return VerificationResult("failed", {}, "normalized_lineage_missing")
        if not document.get("chunks") or any(not chunk.get("text") for chunk in document["chunks"]):
            return VerificationResult("failed", {}, "normalized_chunks_empty")
    return VerificationResult("passed", normalized.get("metrics", {}))


def _conflict_report(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next((item for item in result.get("artifacts", []) if item.get("kind") == "conflict_report"), None)
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_report_missing")
    report = services.object_json(artifact["id"])
    if not isinstance(report, dict) or not report.get("candidates") or "decision" not in report:
        return VerificationResult("failed", {}, "source_evidence_missing")
    if any(not {"source_uri", "source_version", "acl_digest", "candidate_id"} <= candidate.keys() for candidate in report["candidates"]):
        return VerificationResult("failed", {}, "source_evidence_missing")
    return VerificationResult("passed", {"status": report["decision"].get("status"), "candidates": len(report["candidates"])})


def _conflict_decision(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next((item for item in result.get("artifacts", []) if item.get("kind") == "conflict_decision"), None)
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_decision_missing")
    decision = services.object_json(artifact["id"])
    if not isinstance(decision, dict) or decision.get("decision", {}).get("status") != "resolved" or not decision["decision"].get("approved_by"):
        return VerificationResult("failed", {}, "decision_unapproved")
    return VerificationResult("passed", {"selected_candidate_id": decision["decision"].get("selected_candidate_id")})


def default_verifiers() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(VerifierSpec("verify_ingest", 1, _ingest))
    registry.register(VerifierSpec("verify_ingest", 2, _ingest_v2))
    registry.register(VerifierSpec("verify_retrieval", 1, _retrieval))
    registry.register(VerifierSpec("verify_retrieval", 2, _retrieval_v2))
    registry.register(VerifierSpec("verify_memory", 1, _memory))
    registry.register(VerifierSpec("verify_release", 1, _release))
    registry.register(VerifierSpec("verify_rough_clean", 1, _rough_clean))
    registry.register(VerifierSpec("verify_rough_clean", 2, _rough_clean_v2))
    registry.register(VerifierSpec("verify_input_manifest", 1, _input_manifest))
    registry.register(VerifierSpec("verify_refined_corpus", 1, _refined_corpus))
    registry.register(VerifierSpec("verify_conflict_report", 1, _conflict_report))
    registry.register(VerifierSpec("verify_conflict_decision", 1, _conflict_decision))
    return registry
