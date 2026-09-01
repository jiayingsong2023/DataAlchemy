"""Shared WebUI service instances and helpers."""

import json
import uuid
from typing import Any

from fastapi import HTTPException, status

from config import DATABASE_URL, INDEX_VERSION, MODEL_VERSION, S3_BUCKET
from core.agent_runtime import AgentRuntime
from core.evidence import EvidenceService, ObjectNotFound, S3EvidenceStore, canonical_bytes, sha256
from core.jobs import JobService, KubernetesJobBackend
from core.runtime_tools import register_runtime_tools
from core.tool_contracts import ToolRegistry
from harness.experience import record_experience_event
from inference.adapter_runtime import AdapterRuntime
from memory.context import ContextService
from memory.orchestrator import MemoryOrchestrator
from rag.answering import GroundedAnswering
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils

MINIO_BUCKET = S3_BUCKET


tool_registry = ToolRegistry()
_vector_store = VectorStore()
_retriever = Retriever(_vector_store)
_memory = MemoryOrchestrator(DATABASE_URL, _vector_store, _retriever)
_adapter_runtime = AdapterRuntime()
_answering = GroundedAnswering()
_evidence_s3 = S3Utils()
_evidence_store = S3EvidenceStore(MINIO_BUCKET, _evidence_s3.client)
agent_runtime = AgentRuntime(
    DATABASE_URL,
    tool_registry,
    evidence=EvidenceService(
        DATABASE_URL,
        _evidence_store,
        tool_registry.sensitivity,
    ),
    jobs=JobService(DATABASE_URL, KubernetesJobBackend(), _evidence_store),
)


def _load_chat_context(ref: str, expected_sha256: str) -> dict[str, Any]:
    body = _evidence_store.get(ref)
    if sha256(body) != expected_sha256:
        raise ValueError("rag_chat_context_hash_mismatch")
    envelope = json.loads(body)
    actual = sha256(
        canonical_bytes({key: value for key, value in envelope.items() if key != "envelope_sha256"})
    )
    if envelope.get("envelope_sha256") != actual:
        raise ValueError("rag_chat_context_envelope_mismatch")
    return envelope


def _publish_chat_context(
    envelope: dict[str, Any], identity: dict[str, str], run_id: str
) -> tuple[str, str]:
    body = canonical_bytes(envelope)
    digest = sha256(body)
    ref = f"tenants/{identity['tenant_id']}/experiences/runs/{run_id}/contexts/sha256/{digest}.json"
    try:
        existing = _evidence_store.get(ref)
    except ObjectNotFound:
        _evidence_store.put(ref, body)
    else:
        if sha256(existing) != digest:
            raise RuntimeError("rag_chat_context_key_conflict")
    return ref, digest


def _record_chat_result(
    run_context: dict[str, Any],
    identity: dict[str, str],
    envelope: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    task_id = run_context["task_id"]
    prior_calls = {
        event["payload"].get("producer"): event["payload"].get("call_id")
        for event in agent_runtime.events(task_id, identity)
        if event["event_type"] == "experience_event"
        and event["payload"].get("type") == "model_call"
    }
    record_experience_event(
        _evidence_store,
        agent_runtime,
        identity,
        task_id,
        "context_built",
        envelope,
        producer="ContextService.build_context",
    )
    record_experience_event(
        _evidence_store,
        agent_runtime,
        identity,
        task_id,
        "tool_call",
        {"tool": "rag_retrieval", "query": result["query"]},
        producer="AgentC.retriever",
    )
    call_ids = []
    for model_call in result["model_calls"]:
        producer = model_call["component"]
        call_id = str(uuid.uuid4())
        record_experience_event(
            _evidence_store,
            agent_runtime,
            identity,
            task_id,
            "model_call",
            model_call,
            producer=producer,
            call_id=call_id,
            retry_of=(prior_calls.get(producer) if run_context.get("attempt", 1) > 1 else None),
        )
        prior_calls[producer] = call_id
        call_ids.append(call_id)
    response = {
        "schema_version": "rag_chat_response.v1",
        "answer": result["answer"],
        "citations": result["citations"],
        "model_execution": result["model_execution"],
        "model_calls": result["model_calls"],
        "execution_status": result["status"],
        "error_code": result["error_code"],
        "context_sha256": envelope["envelope_sha256"],
    }
    observed = record_experience_event(
        _evidence_store,
        agent_runtime,
        identity,
        task_id,
        "tool_observation",
        response,
        producer="rag_chat@1",
        parent_call_id=call_ids[-1] if call_ids else None,
    )
    return {
        "response_ref": observed["content_ref"],
        "response_sha256": observed["sha256"],
        "snapshot_id": envelope["snapshot_id"],
        "context_ref": run_context["context_ref"],
        "context_sha256": envelope["envelope_sha256"],
        "document_ids": sorted(
            {
                str(item["document_id"])
                for item in envelope["retrieval_context"]
                if item.get("document_id")
            }
        ),
        "citations": result["citations"],
        "status": (
            "failed"
            if result["status"] == "failed"
            else "grounded"
            if result["citations"]
            else "abstained"
        ),
    }


register_runtime_tools(
    tool_registry,
    vector_store=_vector_store,
    memory=_memory,
    chat_adapter_runtime=_adapter_runtime,
    chat_answering=_answering,
    chat_retriever=_retriever,
    chat_context_loader=_load_chat_context,
    chat_result_recorder=_record_chat_result,
)
audit_log = AuditLog(DATABASE_URL)
_context_service_instance = ContextService(DATABASE_URL, retriever=_retriever, memory=_memory)


def _cache_scope(identity: dict) -> str:
    return ":".join((identity["tenant_id"], identity["username"], MODEL_VERSION, INDEX_VERSION))


def _require_admin(identity: dict):
    if identity["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required"
        )


def _require_reviewer(identity: dict):
    if identity["role"] not in {"admin", "reviewer"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")


def _context_service() -> ContextService:
    return _context_service_instance


def _run_details(task: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    tools = agent_runtime.tool_runs(task["task_id"], identity)
    verifications = agent_runtime.verifications(task["task_id"], identity)
    by_step = {item.get("step_id"): item for item in tools}
    verification_by_step: dict[str, list[dict[str, Any]]] = {}
    for item in verifications:
        verification_by_step.setdefault(item["step_id"], []).append(item)
    stages = []
    for index, step in enumerate(task["plan"]):
        run = by_step.get(step["step_id"])
        checks = verification_by_step.get(step["step_id"], [])
        if run and checks and all(item["status"] == "passed" for item in checks):
            state = "passed"
        elif run and run["state"] == "failed":
            state = "failed"
        elif index == task["current_step"]:
            state = "waiting_approval" if task["state"] == "waiting_approval" else "running"
        elif index < task["current_step"]:
            state = "passed"
        else:
            state = "pending"
        stages.append(
            {
                "step": index,
                "step_id": step["step_id"],
                "tool": step["tool"],
                "state": state,
                "metrics": (run or {}).get("result", {}).get("metrics", {}),
                "artifacts": (run or {}).get("result", {}).get("artifacts", []),
                "verifications": checks,
                "failure": (run or {}).get("result", {}).get("failure"),
            }
        )
    future_gates = [
        {
            "name": "feedback",
            "state": "waiting_for_input",
            "reason": "submit feedback for this run",
        },
        {
            "name": "memory",
            "state": "waiting_for_input",
            "reason": "H4 memory distillation and policy run is required",
        },
        {
            "name": "training_candidate",
            "state": "not_eligible",
            "reason": "feedback review and H5 snapshot gate are required",
        },
        {
            "name": "lora",
            "state": "blocked_by_phase",
            "reason": "H5 training and fixed evaluation are required",
        },
        {
            "name": "evaluation",
            "state": "blocked_by_phase",
            "reason": "H5 evaluation gate is required",
        },
        {
            "name": "release",
            "state": "blocked_by_phase",
            "reason": "H5 release governance is required",
        },
    ]
    h5_attempt = None
    try:
        with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT attempt_id, state, config_sha256, snapshot_id, base_evaluation_id, "
                    "candidate_evaluation_id, adapter_id, release_id, last_gate, updated_at "
                    "FROM h5_attempts WHERE run_id = %s AND tenant_id = %s AND active",
                    (task["run_id"], identity["tenant_id"]),
                )
                row = cursor.fetchone()
                h5_attempt = dict(row) if row else None
                cursor.execute(
                    "SELECT gate_name, state, input_artifact_id, input_sha256, output_artifact_id, "
                    "output_sha256, evidence_json, occurred_at FROM run_gate_events "
                    "WHERE run_id = %s AND tenant_id = %s ORDER BY occurred_at",
                    (task["run_id"], identity["tenant_id"]),
                )
                durable_gates = [dict(row) for row in cursor.fetchall()]
    except Exception:
        durable_gates = []
    if durable_gates:
        latest = {}
        for gate in durable_gates:
            latest[gate["gate_name"]] = gate
        future_gates = [
            {
                "name": name,
                "state": gate["state"],
                "evidence": gate.get("evidence_json", {}),
                "input_sha256": gate.get("input_sha256"),
                "output_sha256": gate.get("output_sha256"),
            }
            for name, gate in latest.items()
        ]
    artifacts = [artifact for item in tools for artifact in item["result"].get("artifacts", [])]
    approvals = [
        {
            "event_type": event["event_type"],
            "occurred_at": event["occurred_at"],
            "payload": event["payload"],
        }
        for event in agent_runtime.events(task["task_id"], identity)
        if "approval" in event["event_type"]
    ]
    timeline = [
        {
            "kind": "event",
            "at": event["occurred_at"],
            "type": event["event_type"],
            "payload": event["payload"],
        }
        for event in agent_runtime.events(task["task_id"], identity)
    ]
    timeline.extend(
        {
            "kind": "tool",
            "at": item["started_at"],
            "type": item["tool_name"],
            "step_id": item["step_id"],
            "state": item["state"],
        }
        for item in tools
    )
    return {
        "stages": stages,
        "timeline": sorted(timeline, key=lambda item: (str(item.get("at")), str(item.get("type")))),
        "artifacts": artifacts,
        "approvals": approvals,
        "verifications": verifications,
        "gates": future_gates,
        "h5_attempt": h5_attempt,
        "counts": {
            "steps": len(stages),
            "passed_steps": sum(item["state"] == "passed" for item in stages),
            "artifacts": len(artifacts),
            "verifications": len(verifications),
        },
    }
