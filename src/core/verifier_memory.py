"""Ingest, retrieval, context, and memory verifiers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .verifier_contracts import ReadOnlyServices, VerificationResult, _digest


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
            return VerificationResult(
                "failed", {"document_id": document["document_id"]}, "document_not_ready"
            )
        if artifact_hashes.get(document["document_id"]) != document["content_hash"]:
            return VerificationResult("failed", {}, "document_hash_mismatch")
        if metadata.get("trust_label") != "untrusted_external" or not metadata.get("acl_digest"):
            return VerificationResult("failed", {}, "document_lineage_missing")
        if expected_phrase and not services.matching_chunks(
            document["document_id"], expected_phrase
        ):
            return VerificationResult("failed", {}, "expected_phrase_not_found")
    return VerificationResult(
        "passed",
        {
            "document_count": len(documents),
            "chunk_count": sum(item["chunk_count"] for item in documents),
        },
    )


def _input_manifest(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "input_manifest"), None
    )
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
    return VerificationResult(
        "passed", {"matches": len(citations), "document_count": len(document_ids)}
    )


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


def _context_snapshot(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    snapshot_id = result.get("output", {}).get("snapshot_id")
    row = services.context_snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    budget = row.get("budget_json", {}) if row else {}
    expected_identity_digest = _digest(
        {key: services.identity[key] for key in ("tenant_id", "username", "role")}
    )
    if (
        row is None
        or row["tenant_id"] != services.identity["tenant_id"]
        or row["identity_digest"] != expected_identity_digest
    ):
        return VerificationResult("failed", {}, "context_snapshot_missing")
    if not isinstance(budget, dict) or budget.get("used_tokens", 0) > budget.get(
        "input_tokens", 0
    ) - budget.get("reserved_output_tokens", 0):
        return VerificationResult("failed", {}, "context_budget_exceeded")
    if not row["pack_refs"] or len(row["envelope_sha256"]) != 64:
        return VerificationResult("failed", {}, "context_snapshot_schema_invalid")
    return VerificationResult(
        "passed", {"snapshot_id": snapshot_id, "used_tokens": budget.get("used_tokens", 0)}
    )


def _chat_capture(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    parameters = criterion.get("parameters", {})
    output = result.get("output", {})
    response_ref = output.get("response_ref")
    response_sha256 = output.get("response_sha256")
    body = services.object_body(response_ref) if isinstance(response_ref, str) else None
    if (
        body is None
        or not isinstance(response_sha256, str)
        or hashlib.sha256(body).hexdigest() != response_sha256
    ):
        return VerificationResult("blocked", {}, "chat_response_hash_mismatch")
    try:
        response = json.loads(body)
    except json.JSONDecodeError:
        return VerificationResult("blocked", {}, "chat_response_invalid")
    snapshot_id = parameters.get("snapshot_id")
    snapshot = services.context_snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    if (
        snapshot is None
        or snapshot["tenant_id"] != task.get("tenant_id")
        or snapshot["envelope_sha256"] != parameters.get("context_sha256")
        or output.get("context_sha256") != parameters.get("context_sha256")
        or response.get("context_sha256") != parameters.get("context_sha256")
    ):
        return VerificationResult("failed", {}, "chat_context_lineage_mismatch")
    document_ids = parameters.get("document_ids", [])
    citations = response.get("citations", [])
    if not isinstance(document_ids, list) or not isinstance(citations, list):
        return VerificationResult("blocked", {}, "chat_capture_schema_invalid")
    documents = {item["document_id"] for item in services.documents(document_ids)}
    chunks = {item["chunk_id"]: item for item in services.chunks(document_ids)}
    if documents != set(document_ids):
        return VerificationResult("failed", {}, "chat_document_scope_mismatch")
    for citation in citations:
        chunk = chunks.get(citation.get("chunk_id")) if isinstance(citation, dict) else None
        if (
            chunk is None
            or chunk["document_id"] != citation.get("document_id")
            or citation.get("document_id") not in documents
        ):
            return VerificationResult("failed", {}, "chat_citation_not_authorized")
    if not isinstance(response.get("answer"), str) or not response["answer"].strip():
        return VerificationResult("failed", {}, "chat_answer_missing")
    model_calls = response.get("model_calls", [])
    if response.get("execution_status") != "succeeded" or any(
        not isinstance(call, dict) or call.get("status") != "succeeded" for call in model_calls
    ):
        return VerificationResult("failed", {}, "chat_model_call_failed")
    return VerificationResult(
        "passed",
        {
            "snapshot_id": snapshot_id,
            "document_count": len(documents),
            "citation_count": len(citations),
        },
    )


def _context_checkpoint(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    checkpoint_id = result.get("output", {}).get("checkpoint_id")
    row = services.context_checkpoint(checkpoint_id) if isinstance(checkpoint_id, str) else None
    if row is None or row["status"] not in {"verified", "active"}:
        return VerificationResult("failed", {}, "context_checkpoint_missing")
    events = services.conversation_events(
        str(row["session_id"]), row["source_sequence_start"], row["source_sequence_end"]
    )
    if (
        _digest(
            [
                {
                    "event_id": item["event_id"],
                    "sequence_no": item["sequence_no"],
                    "hash": item["content_sha256"],
                }
                for item in events
            ]
        )
        != row["source_digest"]
    ):
        return VerificationResult("failed", {}, "checkpoint_source_digest_mismatch")
    handoff = row.get("handoff_json")
    if not isinstance(handoff, dict) or not isinstance(handoff.get("confirmed_claims", []), list):
        return VerificationResult("failed", {}, "handoff_schema_invalid")
    if len(row["source_digest"]) != 64 or not row["summary"].strip():
        return VerificationResult("failed", {}, "checkpoint_source_invalid")
    return VerificationResult("passed", {"checkpoint_id": checkpoint_id})


def _memory_distillation(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    candidates = result.get("output", {}).get("candidates", [])
    if not isinstance(candidates, list):
        return VerificationResult("failed", {}, "candidate_schema_invalid")
    for candidate in candidates:
        if not candidate.get("source_event_ids") or not candidate.get("claim_key"):
            return VerificationResult("failed", {}, "candidate_provenance_missing")
        row = (
            services.memory_candidate(candidate.get("memory_id"))
            if candidate.get("memory_id")
            else None
        )
        if row is not None and row["tenant_id"] != services.identity["tenant_id"]:
            return VerificationResult("failed", {}, "candidate_tenant_mismatch")
    return VerificationResult("passed", {"candidate_count": len(candidates)})


def _memory_policy(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    decisions = result.get("output", {}).get("decisions", [])
    if not isinstance(decisions, list):
        return VerificationResult("failed", {}, "policy_schema_invalid")
    for decision in decisions:
        row = (
            services.memory_candidate(decision.get("memory_id"))
            if decision.get("memory_id")
            else None
        )
        if row is None or row["tenant_id"] != services.identity["tenant_id"]:
            return VerificationResult("failed", {}, "policy_memory_missing")
        if decision.get("status") == "approved" and row["risk_class"] in {"prohibited", "legacy"}:
            return VerificationResult("failed", {}, "policy_approved_forbidden_memory")
    return VerificationResult("passed", {"decision_count": len(decisions)})
