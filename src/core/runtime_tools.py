"""Register governed runtime tools."""

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable

from config import (
    DATABASE_URL,
    GIT_PILOT_READERS,
    GIT_PILOT_REPOSITORY,
    GIT_PILOT_TOKEN,
    PILOT_RUNS_DIR,
    S3_BUCKET,
)
from connectors.git import GitConnector
from connectors.git_ingestion import prepare_git_document
from harness.evaluation import EvaluationService
from harness.product_loop import (
    digest,
    refine_records,
    rough_records,
    sha256_bytes,
)
from memory.context import ContextService
from rag.answering import answer_with_citations
from release.governance import ReleaseGovernance
from storage.audit import AuditLog
from utils.s3_utils import S3Utils

from .agent_runtime import ToolRegistry, ToolSpec


def _document_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    key = arguments["object_key"].removeprefix("raw/documents/")
    return [f"raw:document:{key}"]


def _git_scope(_arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"connector:git:{GIT_PILOT_REPOSITORY}"]


def _rough_clean_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"raw:{arguments['input_key']}"]


def _h3_input_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"raw:{arguments['input_key']}"]


def _h3_artifact_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"raw:{arguments['input_key']}"]


def _h3_publish_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"raw:{arguments['input_key']}", f"postgres:tenant:{_identity['tenant_id']}"]


def _context_session_scope(arguments: dict[str, Any], _identity: dict[str, str]) -> list[str]:
    return [f"session:{arguments['session_id']}"]


def _context_policy_scope(_arguments: dict[str, Any], identity: dict[str, str]) -> list[str]:
    return [f"tenant:{identity['tenant_id']}"]


def _h5_scope(_arguments: dict[str, Any], identity: dict[str, str]) -> list[str]:
    return [f"h5:tenant:{identity['tenant_id']}"]


def _s3_parts(key: str) -> tuple[S3Utils, str]:
    normalized = key.replace("s3a://", "s3://", 1)
    if normalized.startswith("s3://"):
        bucket, _, object_key = normalized.removeprefix("s3://").partition("/")
        if not bucket or not object_key:
            raise ValueError("S3 artifact key is incomplete")
        return S3Utils(bucket=bucket), object_key
    return S3Utils(), normalized


def _put_json(store: S3Utils, key: str, value: Any, kind: str) -> dict[str, Any]:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if not store.put_object(key, body, "application/json"):
        raise RuntimeError(f"artifact_write_failed:{kind}")
    return {
        "store": "minio",
        "kind": kind,
        "id": key,
        "sha256": sha256_bytes(body),
        "size": len(body),
    }


def _read_json_lines(store: S3Utils, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in sorted(
        store.list_objects(prefix.rstrip("/") + "/"), key=lambda value: value["Key"]
    ):
        key = item["Key"]
        if not key.endswith((".json", ".jsonl")):
            continue
        body = store.get_object_body(key)
        if not body:
            continue
        for line in body.decode("utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _h3_context(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments.pop("_h3_context", {})


def _prior_artifact(context: dict[str, Any], kind: str) -> str:
    for artifact in reversed(context.get("previous_artifacts", [])):
        if artifact.get("store") == "minio" and artifact.get("kind") == kind:
            return artifact["id"]
    raise ValueError(f"prior_artifact_missing:{kind}")


def _belongs_to_run(key: str, run_id: str) -> bool:
    normalized = key.replace("s3a://", "s3://", 1)
    object_key = (
        normalized.removeprefix("s3://").split("/", 1)[1]
        if normalized.startswith("s3://") and "/" in normalized.removeprefix("s3://")
        else normalized
    )
    return object_key.startswith(f"runs/{run_id}/")


def _validate_document_input(_coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    _h3_context(arguments)
    input_key = arguments["input_key"]
    if not input_key.startswith(f"raw/harness/{identity['tenant_id']}/") or not input_key.endswith(
        "/input.json"
    ):
        raise PermissionError("input descriptor is outside the tenant harness prefix")
    store, descriptor_key = _s3_parts(input_key)
    descriptor_body = store.get_object_body(descriptor_key)
    if not descriptor_body:
        raise FileNotFoundError("input descriptor was not found")
    descriptor = json.loads(descriptor_body)
    source = descriptor.get("source", {})
    raw_key = source.get("object_key")
    if not raw_key:
        raw_key = input_key.removesuffix("/input.json") + "/documents/" + source["filename"]
    raw_store, raw_object_key = _s3_parts(raw_key)
    body = raw_store.get_object_body(raw_object_key)
    if body is None or sha256_bytes(body) != arguments["input_sha256"]:
        raise ValueError("input_hash_mismatch")
    if (
        descriptor.get("tenant_id") != identity["tenant_id"]
        or descriptor.get("owner") != identity["username"]
    ):
        raise PermissionError("input_identity_mismatch")
    artifact = {
        "store": "minio",
        "kind": "input_manifest",
        "id": input_key,
        "sha256": sha256_bytes(descriptor_body),
        "size": len(descriptor_body),
    }
    return {
        "input_id": descriptor["input_id"],
        "source_version": source["version"],
        "source_uri": source["uri"],
        "input_sha256": arguments["input_sha256"],
        "acl_digest": descriptor["acl_digest"],
        "observed_scope": [f"raw:{input_key}"],
        "artifacts": [artifact],
        "metrics": {"bytes": len(body), "accepted": 1, "rejected": 0},
    }


def _refine_corpus(_coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    context = _h3_context(arguments)
    artifact_key = _prior_artifact(context, "cleaned_corpus")
    if not _belongs_to_run(artifact_key, context.get("run_id", "")):
        raise PermissionError("rough artifact is outside the run scope")
    input_key = arguments["input_key"]
    input_store, input_object = _s3_parts(input_key)
    descriptor_body = input_store.get_object_body(input_object)
    if not descriptor_body:
        raise FileNotFoundError("input descriptor was not found")
    descriptor = json.loads(descriptor_body)
    if descriptor.get("tenant_id") != identity["tenant_id"]:
        raise PermissionError("input_tenant_mismatch")
    store, prefix = _s3_parts(artifact_key)
    rows = _read_json_lines(store, prefix)
    if not rows:
        raise ValueError("rough_corpus_empty")
    source_uri = descriptor["source"]["uri"]
    parsed_rows = []
    for row in rows:
        if row.get("source_name") != "documents" or not row.get("text"):
            continue
        row_acl = row.get("acl_digest")
        if row_acl and row_acl != descriptor["acl_digest"]:
            raise ValueError("rough_acl_mismatch")
        parsed_rows.append(
            {
                "page": row.get("page"),
                "paragraph": row.get("paragraph"),
                "text": row["text"],
                "injection_codes": row.get("reason_codes", [])
                if row.get("decision") == "quarantined"
                else [],
            }
        )
    accepted, quarantined = rough_records(parsed_rows, descriptor, source_uri)
    normalized = refine_records(accepted, descriptor, source_uri)
    output_key = f"runs/{context['run_id']}/h3/{context['step_id']}/normalized_documents.json"
    artifact = _put_json(store, output_key, normalized, "normalized_documents")
    quarantine_key = f"runs/{context['run_id']}/h3/{context['step_id']}/quarantine.json"
    quarantine_artifact = _put_json(store, quarantine_key, {"records": quarantined}, "quarantine")
    return {
        "input_id": descriptor["input_id"],
        "artifact_key": output_key,
        "source_version": descriptor["source"]["version"],
        "observed_scope": [f"raw:{input_key}"],
        "artifacts": [artifact, quarantine_artifact],
        "metrics": {
            "accepted": len(accepted),
            "quarantined": len(quarantined),
            "documents": normalized["metrics"]["documents"],
            "chunks": normalized["metrics"]["chunks"],
        },
    }


def _publish_corpus(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    context = _h3_context(arguments)
    artifact_key = _prior_artifact(context, "normalized_documents")
    if not _belongs_to_run(artifact_key, context.get("run_id", "")):
        raise PermissionError("normalized artifact is outside the run scope")
    store, object_key = _s3_parts(artifact_key)
    body = store.get_object_body(object_key)
    if not body:
        raise FileNotFoundError("normalized artifact was not found")
    normalized = json.loads(body)
    expected_digest = normalized.pop("sha256", None)
    if expected_digest != digest(normalized):
        raise ValueError("normalized_artifact_hash_mismatch")
    if normalized.get("tenant_id") != identity["tenant_id"]:
        raise PermissionError("normalized_tenant_mismatch")
    documents = []
    for item in normalized.get("documents", []):
        acl = [(entry["subject_type"], entry["subject_id"]) for entry in item.get("acl", [])]
        chunks = [
            {
                "text": chunk["text"],
                "metadata": {
                    "source": item["source_uri"],
                    "source_uri": item["source_uri"],
                    "source_version": item["source_version"],
                    "document_key": item["document_key"],
                    "locator": chunk["locator"],
                    "acl_digest": item["acl_digest"],
                    "trust_label": item["trust_label"],
                },
            }
            for chunk in item.get("chunks", [])
        ]
        documents.append(
            {
                "text": "\n".join(chunk["text"] for chunk in chunks),
                "source": item["source_uri"],
                "metadata": {
                    "source": item["source_uri"],
                    "source_uri": item["source_uri"],
                    "source_version": item["source_version"],
                    "document_key": item["document_key"],
                    "acl": acl,
                    "acl_digest": item["acl_digest"],
                    "trust_label": item["trust_label"],
                },
                "chunks": chunks,
                "content_hash": item["content_hash"],
            }
        )
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    document_ids = coordinator.agent_manager.agent_c.vs.add_documents(documents, identity, None)
    artifacts = [
        {"store": "postgres", "kind": "document", "id": document_id, "sha256": item["content_hash"]}
        for document_id, item in zip(document_ids, normalized["documents"], strict=True)
    ]
    return {
        "document_ids": document_ids,
        "source_version": normalized["source_version"],
        "observed_scope": [
            f"raw:{arguments['input_key']}",
            f"postgres:tenant:{identity['tenant_id']}",
        ],
        "artifacts": artifacts,
        "metrics": {
            "accepted": len(document_ids),
            "rejected": 0,
            "chunks": normalized["metrics"]["chunks"],
        },
    }


def _rag_probe(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    context = _h3_context(arguments)
    query = arguments["query"].strip()
    if not query:
        raise ValueError("query_empty")
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    candidates = coordinator.agent_manager.agent_c.retriever.retrieve(query, identity, top_k=5)
    document_ids = list(
        dict.fromkeys(item.get("document_id") for item in candidates if item.get("document_id"))
    )
    citations = [
        {
            "chunk_id": item["chunk_id"],
            "document_id": item.get("document_id"),
            "source_uri": item.get("source"),
            "source_version": item.get("document_version"),
            "source_sha256": str(
                item.get("metadata", {}).get("source_version") or item.get("document_version") or ""
            ).removeprefix("sha256:"),
            "locator": item.get("metadata", {}).get("locator"),
            "run_id": context.get("run_id"),
        }
        for item in candidates
    ]
    report = {"query": query, "document_ids": document_ids, "citations": citations}
    store = S3Utils()
    key = f"runs/{context['run_id']}/h3/{context['step_id']}/retrieval_report.json"
    artifact = _put_json(store, key, report, "retrieval_report")
    return {
        "query": query,
        "document_ids": document_ids,
        "chunk_ids": [item["chunk_id"] for item in candidates],
        "citations": citations,
        "observed_scope": [f"postgres:tenant:{identity['tenant_id']}"],
        "artifacts": [artifact],
        "metrics": {"citation_count": len(citations)},
    }


def _compare_sources(_coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    context = _h3_context(arguments)
    candidates = arguments.get("candidates", [])
    if not candidates:
        raise ValueError("source_candidates_missing")
    normalized = []
    for index, candidate in enumerate(candidates):
        if (
            not isinstance(candidate, dict)
            or not {"value", "source_uri", "source_version", "acl_digest"} <= candidate.keys()
        ):
            raise ValueError("source_evidence_missing")
        normalized.append({"candidate_id": str(index), **candidate})
    values = {json.dumps(item["value"], sort_keys=True, ensure_ascii=False) for item in normalized}
    status = "resolved" if len(values) == 1 else "needs_approval"
    decision = {"status": status, "rule_id": "same_value_v1" if status == "resolved" else None}
    if status == "resolved":
        decision["selected_candidate_id"] = normalized[0]["candidate_id"]
    report = {"claim_key": arguments["claim_key"], "candidates": normalized, "decision": decision}
    key = f"runs/{context['run_id']}/h3/{context['step_id']}/conflict_report.json"
    artifact = _put_json(S3Utils(), key, report, "conflict_report")
    return {
        "decision_status": status,
        "report_key": key,
        "observed_scope": [f"postgres:tenant:{identity['tenant_id']}"],
        "artifacts": [artifact],
        "metrics": {"conflicts": 0 if status == "resolved" else 1},
    }


def _resolve_conflict(_coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    context = _h3_context(arguments)
    report_key = arguments["report_key"]
    store, object_key = _s3_parts(report_key)
    body = store.get_object_body(object_key)
    if not body:
        raise FileNotFoundError("conflict report not found")
    report = json.loads(body)
    candidate_id = arguments["candidate_id"]
    candidates = {item["candidate_id"] for item in report.get("candidates", [])}
    if candidate_id not in candidates:
        raise ValueError("conflict_candidate_invalid")
    decision = {
        "status": "resolved",
        "selected_candidate_id": candidate_id,
        "approved_by": identity["username"],
    }
    key = f"runs/{context['run_id']}/h3/{context['step_id']}/conflict_decision.json"
    artifact = _put_json(
        S3Utils(),
        key,
        {"claim_key": report["claim_key"], "decision": decision},
        "conflict_decision",
    )
    return {
        "decision_status": "resolved",
        "selected_candidate_id": candidate_id,
        "observed_scope": [f"artifact:{report_key}"],
        "artifacts": [artifact],
        "metrics": {"approved": 1},
    }


def _document_result(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("document_ids"), list) or not payload["document_ids"]:
        raise ValueError("ingest_document must return document_ids")


def _ingest_document(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Publish one already-landed Markdown/TXT object after an approved task."""
    identity = arguments.pop("_identity")
    object_key = arguments["object_key"].strip()
    if not object_key.startswith("raw/documents/") or object_key == "raw/documents/":
        raise ValueError("object_key must be under raw/documents/")
    raw = S3Utils().get_object_body(object_key)
    if raw is None:
        raise RuntimeError("raw document was not found in object storage")
    filename = object_key.rsplit("/", 1)[-1]
    document, chunker, rejection = prepare_git_document(
        filename,
        raw,
        f"s3://{S3_BUCKET}/{object_key}",
        {"raw_object_key": object_key, "source_type": "pilot_document"},
    )
    if rejection:
        raise ValueError(f"document rejected: {rejection}")
    assert document is not None and chunker is not None
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    document_ids = coordinator.agent_manager.agent_c.vs.add_documents([document], identity, chunker)
    AuditLog(DATABASE_URL).record(
        identity,
        "document.ingest",
        "document",
        resource_id=document_ids[0],
        metadata={"object_key": object_key},
    )
    content_hash = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
    scope = f"raw:document:{object_key.removeprefix('raw/documents/')}"
    return {
        "document_id": document_ids[0],
        "document_ids": document_ids,
        "object_key": object_key,
        "observed_scope": [scope],
        "artifacts": [
            {
                "store": "postgres",
                "kind": "document",
                "id": document_ids[0],
                "version": 1,
                "sha256": content_hash,
            }
        ],
        "metrics": {"accepted": 1, "rejected": 0},
    }


def _sync_git(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    if not GIT_PILOT_REPOSITORY:
        raise RuntimeError("GIT_PILOT_REPOSITORY is required")
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    readers = [("user", name.strip()) for name in GIT_PILOT_READERS.split(",") if name.strip()]
    result = GitConnector(DATABASE_URL, GIT_PILOT_REPOSITORY, GIT_PILOT_TOKEN).sync(
        identity,
        vector_store=coordinator.agent_manager.agent_c.vs,
        acl=readers,
        runs_dir=PILOT_RUNS_DIR,
    )
    return {
        **result,
        "operation_ref": result["connector_run_id"],
        "observed_scope": [f"connector:git:{GIT_PILOT_REPOSITORY}"],
    }


def register_coordinator_tools(
    registry: ToolRegistry,
    coordinator: Any,
    *,
    chat_adapter_runtime: Any,
    chat_answering: Any,
    chat_retriever: Any,
    chat_context_loader: Callable[[str, str], dict[str, Any]] | None = None,
    chat_result_recorder: Callable[
        [dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]], dict[str, Any]
    ]
    | None = None,
) -> None:
    def compact_context(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        service = ContextService(DATABASE_URL)
        result = service.compact(
            arguments["session_id"], identity, summary=arguments.get("summary")
        )
        return {**result, "observed_scope": [f"session:{arguments['session_id']}"]}

    def distill_memory_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        service = ContextService(DATABASE_URL)
        session_id = arguments["session_id"]
        events = service.events(session_id, identity)
        candidates = service.extract_candidates(events)
        coordinator.agent_manager.lazy_load_agents(need_c=True)
        memory = coordinator.agent_manager.agent_c.memory
        session = service.get_session(session_id, identity)
        decisions = []
        for item in candidates:
            decision = memory.create_governed_candidate(
                identity, item, auto_memory_enabled=session["auto_memory_enabled"]
            )
            decisions.append({**item, **decision})
        return {
            "candidates": decisions,
            "session_id": session_id,
            "observed_scope": [f"session:{session_id}"],
        }

    def apply_memory_policy(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        coordinator.agent_manager.lazy_load_agents(need_c=True)
        memory = coordinator.agent_manager.agent_c.memory
        decisions = []
        for memory_id in arguments.get("memory_ids", []):
            rows = memory.list(identity)
            row = next((item for item in rows if item["memory_id"] == memory_id), None)
            if row is None:
                raise PermissionError("memory candidate is outside the tenant scope")
            decisions.append(
                {"memory_id": memory_id, "status": row["status"], "reason": row["decision_reason"]}
            )
        return {"decisions": decisions, "observed_scope": [f"tenant:{identity['tenant_id']}"]}

    for name, handler, required in (
        ("compact_context", compact_context, ["session_id"]),
        ("distill_memory_candidates", distill_memory_candidates, ["session_id"]),
        ("apply_memory_policy", apply_memory_policy, []),
    ):
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema={
                    "type": "object",
                    "required": required,
                    "properties": {
                        "session_id": {"type": "string"},
                        "memory_ids": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                idempotent=True,
                side_effecting=name != "apply_memory_policy",
                uses_identity=True,
                scope_resolver=_context_policy_scope
                if name == "apply_memory_policy"
                else _context_session_scope,
                result_sensitivity={"*": "internal"},
            )
        )

    async def chat(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        run_context = arguments.pop("_h3_context", {})
        context_ref = arguments.get("context_ref")
        if context_ref is None:
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ValueError("rag_chat_query_missing")
            context = await asyncio.to_thread(chat_retriever.retrieve, query, identity, top_k=3)
            answer, _, _ = await answer_with_citations(
                query,
                identity,
                context,
                chat_adapter_runtime,
                chat_answering,
            )
            return {"answer": answer}
        if not context_ref.startswith(f"tenants/{identity['tenant_id']}/"):
            raise PermissionError("rag_chat_context_tenant_mismatch")
        if chat_context_loader is None or chat_result_recorder is None:
            raise RuntimeError("rag_chat_capture_not_configured")
        envelope = chat_context_loader(context_ref, arguments["context_sha256"])
        query = envelope.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("rag_chat_query_missing")
        started = time.perf_counter()
        model_calls: list[dict[str, Any]] = []
        try:
            answer, citations, model_execution = await answer_with_citations(
                query,
                identity,
                envelope["retrieval_context"],
                chat_adapter_runtime,
                chat_answering,
                trace_recorder=model_calls.append,
            )
        except Exception as error:
            chat_result_recorder(
                {**run_context, "context_ref": context_ref},
                identity,
                envelope,
                {
                    "answer": "",
                    "citations": [],
                    "model_execution": {},
                    "query": query,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "model_calls": model_calls,
                    "status": "failed",
                    "error_code": type(error).__name__,
                },
            )
            raise
        return chat_result_recorder(
            {**run_context, "context_ref": context_ref},
            identity,
            envelope,
            {
                "answer": answer,
                "citations": citations,
                "model_execution": model_execution,
                "query": query,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "model_calls": model_calls,
                "status": "succeeded",
                "error_code": None,
            },
        )

    def ingest(arguments: dict[str, Any]) -> dict[str, str]:
        coordinator.run_ingestion_pipeline(
            stage=arguments.get("stage", "all"),
            synthesis=arguments.get("synthesis", False),
            max_samples=arguments.get("max_samples"),
        )
        return {"status": "completed"}

    def train(_: dict[str, Any]) -> dict[str, str]:
        coordinator.run_training_pipeline()
        return {"status": "completed"}

    def release(_: dict[str, Any]) -> dict[str, str]:
        if not coordinator.reload_model():
            raise RuntimeError("Model reload failed")
        return {"status": "completed"}

    def evaluate(_: dict[str, Any]) -> dict[str, str]:
        script = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_phase1_baseline.py"
        completed = subprocess.run(
            [sys.executable, str(script)], check=False, capture_output=True, text=True
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "Phase 1 evaluation failed")
        return {"status": "completed", "summary": completed.stdout.strip()}

    def h5_create_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        snapshot_id = service.create_snapshot(identity, **arguments)
        return {"snapshot_id": snapshot_id, "status": "candidate"}

    def h5_create_evaluation(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        evaluation_id = service.create_campaign(
            identity,
            arguments["suite"],
            subject_type=arguments["subject_type"],
            subject_ref=arguments["subject_ref"],
            required_trials=arguments.get("required_trials", 3),
        )
        return {"evaluation_id": evaluation_id, "status": "draft"}

    def h5_approve_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.approve_snapshot(identity, arguments["snapshot_id"])
        return {"snapshot_id": arguments["snapshot_id"], "status": "approved"}

    def h5_revoke_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.revoke_snapshot(identity, arguments["snapshot_id"], arguments["reason"])
        return {"snapshot_id": arguments["snapshot_id"], "status": "revoked"}

    def h5_create_adapter(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        adapter_id = service.create_adapter_candidate(identity, **arguments)
        return {"adapter_id": adapter_id, "status": "candidate"}

    def h5_verify_adapter(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.verify_adapter(identity, arguments["adapter_id"], arguments["evaluation_id"])
        return {"adapter_id": arguments["adapter_id"], "status": "verified"}

    def h5_create_release_candidate(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        release_id = ReleaseGovernance(DATABASE_URL).create_candidate(
            identity, arguments["manifest"]
        )
        return {"release_id": release_id, "status": "candidate"}

    def h5_advance_release(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        return ReleaseGovernance(DATABASE_URL).advance(
            arguments["release_id"],
            arguments["target"],
            identity,
            arguments.get("expected_version"),
        )

    def h5_observe_release(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        status = ReleaseGovernance(DATABASE_URL).observe(
            arguments["release_id"], arguments["metrics"], identity
        )
        return {"release_id": arguments["release_id"], "status": status}

    registry.register(
        ToolSpec(
            name="rag_chat",
            handler=chat,
            schema={
                "type": "object",
                "required": [],
                "properties": {
                    "query": {"type": "string"},
                    "context_ref": {"type": "string"},
                    "context_sha256": {"type": "string"},
                },
                "additionalProperties": False,
            },
            timeout_seconds=300,
            uses_identity=True,
            idempotent=True,
            max_retries=1,
            scope_resolver=lambda arguments, _identity: (
                [arguments["context_ref"]] if arguments.get("context_ref") else []
            ),
            result_sensitivity={
                "answer": "secret",
                "response_ref": "internal",
                "response_sha256": "internal",
                "snapshot_id": "internal",
                "context_ref": "internal",
                "context_sha256": "internal",
                "document_ids": "internal",
                "citations": "internal",
                "status": "public",
            },
        )
    )
    registry.register(
        ToolSpec(
            name="spark_rough_clean",
            handler=lambda _arguments: (_ for _ in ()).throw(
                RuntimeError("Spark jobs are not inline tools")
            ),
            schema={
                "type": "object",
                "required": ["input_key", "input_sha256"],
                "properties": {
                    "input_key": {"type": "string"},
                    "input_sha256": {"type": "string"},
                    "deadline_seconds": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            version=1,
            execution="kubernetes_job",
            job_kind="spark_rough_clean",
            scope_resolver=_rough_clean_scope,
            expected_artifacts=frozenset({("minio", "cleaned_corpus")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="validate_document_input",
            handler=partial(_validate_document_input, coordinator),
            schema={
                "type": "object",
                "required": ["input_key", "input_sha256"],
                "properties": {
                    "input_key": {"type": "string"},
                    "input_sha256": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=_h3_input_scope,
            expected_artifacts=frozenset({("minio", "input_manifest")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="refine_corpus",
            handler=partial(_refine_corpus, coordinator),
            schema={
                "type": "object",
                "required": ["input_key"],
                "properties": {
                    "input_key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=_h3_artifact_scope,
            expected_artifacts=frozenset(
                {("minio", "normalized_documents"), ("minio", "quarantine")}
            ),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="publish_corpus",
            handler=partial(_publish_corpus, coordinator),
            schema={
                "type": "object",
                "required": ["input_key"],
                "properties": {"input_key": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=_h3_publish_scope,
            expected_artifacts=frozenset({("postgres", "document")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="rag_probe",
            handler=partial(_rag_probe, coordinator),
            schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"user", "admin"}),
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=lambda _arguments, identity: [
                f"postgres:tenant:{identity['tenant_id']}"
            ],
            expected_artifacts=frozenset({("minio", "retrieval_report")}),
            result_sensitivity={"citations": "public", "*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="compare_sources",
            handler=partial(_compare_sources, coordinator),
            schema={
                "type": "object",
                "required": ["claim_key", "candidates"],
                "properties": {
                    "claim_key": {"type": "string"},
                    "candidates": {"type": "array"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=lambda _arguments, identity: [
                f"postgres:tenant:{identity['tenant_id']}"
            ],
            expected_artifacts=frozenset({("minio", "conflict_report")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="resolve_conflict",
            handler=partial(_resolve_conflict, coordinator),
            schema={
                "type": "object",
                "required": ["report_key", "candidate_id"],
                "properties": {
                    "report_key": {"type": "string"},
                    "candidate_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=lambda arguments, _identity: [f"artifact:{arguments['report_key']}"],
            expected_artifacts=frozenset({("minio", "conflict_decision")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="ingest_document",
            handler=partial(_ingest_document, coordinator),
            schema={
                "type": "object",
                "required": ["object_key"],
                "properties": {"object_key": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=2,
            scope_resolver=_document_scope,
            result_validator=_document_result,
            expected_artifacts=frozenset({("postgres", "document")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="ingest",
            handler=ingest,
            schema={
                "type": "object",
                "properties": {
                    "stage": {"type": "string"},
                    "synthesis": {"type": "boolean"},
                    "max_samples": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            blocked_reason="requires H2 job evidence",
        )
    )
    for name, handler in {"train": train, "evaluate": evaluate, "release": release}.items():
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema={"type": "object", "additionalProperties": False},
                roles=frozenset({"admin"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=name in {"train", "release"},
                blocked_reason="requires H2/H5 evidence and release gates",
            )
        )
    registry.register(
        ToolSpec(
            name="h5_create_evaluation",
            handler=h5_create_evaluation,
            schema={
                "type": "object",
                "required": ["suite", "subject_type", "subject_ref"],
                "properties": {
                    "suite": {"type": "object"},
                    "subject_type": {"type": "string"},
                    "subject_ref": {"type": "string"},
                    "required_trials": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_create_snapshot",
            handler=h5_create_snapshot,
            schema={
                "type": "object",
                "required": [
                    "annotation_items",
                    "dataset_key",
                    "dataset_sha256",
                    "dataset_size",
                    "base_model_digest",
                    "policy_version",
                ],
                "properties": {
                    "annotation_items": {"type": "array"},
                    "dataset_key": {"type": "string"},
                    "dataset_sha256": {"type": "string"},
                    "dataset_size": {"type": "integer"},
                    "base_model_digest": {"type": "string"},
                    "policy_version": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    for name, handler, schema in (
        (
            "h5_approve_snapshot",
            h5_approve_snapshot,
            {
                "type": "object",
                "required": ["snapshot_id"],
                "properties": {"snapshot_id": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        (
            "h5_revoke_snapshot",
            h5_revoke_snapshot,
            {
                "type": "object",
                "required": ["snapshot_id", "reason"],
                "properties": {"snapshot_id": {"type": "string"}, "reason": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        (
            "h5_verify_adapter",
            h5_verify_adapter,
            {
                "type": "object",
                "required": ["adapter_id", "evaluation_id"],
                "properties": {
                    "adapter_id": {"type": "string"},
                    "evaluation_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
    ):
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema=schema,
                roles=frozenset({"admin", "reviewer"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=True,
                uses_identity=True,
                scope_resolver=_h5_scope,
                result_sensitivity={"*": "internal"},
            )
        )
    registry.register(
        ToolSpec(
            name="h5_create_adapter",
            handler=h5_create_adapter,
            schema={
                "type": "object",
                "required": [
                    "snapshot_id",
                    "base_model_digest",
                    "tokenizer_digest",
                    "artifact_key",
                    "artifact_sha256",
                    "artifact_size",
                    "config",
                    "environment",
                    "safety_scan",
                ],
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "base_model_digest": {"type": "string"},
                    "tokenizer_digest": {"type": "string"},
                    "artifact_key": {"type": "string"},
                    "artifact_sha256": {"type": "string"},
                    "artifact_size": {"type": "integer"},
                    "config": {"type": "object"},
                    "environment": {"type": "object"},
                    "safety_scan": {"type": "object"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_create_release_candidate",
            handler=h5_create_release_candidate,
            schema={
                "type": "object",
                "required": ["manifest"],
                "properties": {"manifest": {"type": "object"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_advance_release",
            handler=h5_advance_release,
            schema={
                "type": "object",
                "required": ["release_id", "target"],
                "properties": {
                    "release_id": {"type": "string"},
                    "target": {"type": "string"},
                    "expected_version": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_observe_release",
            handler=h5_observe_release,
            schema={
                "type": "object",
                "required": ["release_id", "metrics"],
                "properties": {
                    "release_id": {"type": "string"},
                    "metrics": {"type": "object"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    for name, kind in (("h5_train_lora", "lora_train"), ("h5_model_evaluate", "model_evaluate")):
        registry.register(
            ToolSpec(
                name=name,
                handler=lambda _arguments: (_ for _ in ()).throw(
                    RuntimeError("H5 Kubernetes job must not execute inline")
                ),
                schema={
                    "type": "object",
                    "required": ["input_key", "input_sha256"],
                    "properties": {
                        "input_key": {"type": "string"},
                        "input_sha256": {"type": "string"},
                        "deadline_seconds": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                roles=frozenset({"admin"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=True,
                uses_identity=True,
                scope_resolver=_h5_scope,
                execution="kubernetes_job",
                job_kind=kind,
                result_sensitivity={"*": "internal"},
            )
        )
    registry.register(
        ToolSpec(
            name="sync_git",
            handler=partial(_sync_git, coordinator),
            schema={"type": "object", "additionalProperties": False},
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=60,
            max_calls_per_minute=6,
            max_retries=1,
            sensitive_fields=frozenset({"token", "authorization"}),
            version=2,
            scope_resolver=_git_scope,
            result_sensitivity={"*": "internal"},
        )
    )
