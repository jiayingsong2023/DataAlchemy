import asyncio
import datetime
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import timedelta


# Suppress noisy Windows Proactor errors and unwanted 404s
class LogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Suppress Windows Connection Reset (10054)
        if "10054" in msg:
            return False
        # Suppress the old API status 404 while browser caches clear
        if "/api/status" in msg and "404" in msg:
            return False
        return True


logging.getLogger("uvicorn.error").addFilter(LogFilter())
logging.getLogger("uvicorn.access").addFilter(LogFilter())

from typing import Any, Optional

import boto3
from botocore.client import Config
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from utils.logger import logger

# Add src directory to path to import Coordinator
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from fastapi.security import OAuth2PasswordRequestForm

from agents.coordinator import Coordinator
from config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_MODE,
    DATABASE_URL,
    INDEX_VERSION,
    MODEL_VERSION,
    validate_config,
)
from config import (
    S3_ACCESS_KEY as MINIO_ACCESS_KEY,
)
from config import (
    S3_BUCKET as MINIO_BUCKET,
)
from config import (
    S3_ENDPOINT as MINIO_ENDPOINT,
)
from config import (
    S3_SECRET_KEY as MINIO_SECRET_KEY,
)
from core.agent_runtime import AgentRuntime, ToolRegistry
from core.evidence import EvidenceService, ObjectNotFound, S3EvidenceStore, canonical_bytes, sha256
from core.jobs import JobService, KubernetesJobBackend
from core.runtime_tools import register_runtime_tools
from harness.evaluation import EvaluationService
from harness.experience import record_experience_event
from harness.pilot import PilotService
from harness.product_loop import (
    DocumentRejected,
    build_input_descriptor,
    sha256_bytes,
    validate_upload,
)
from harness.qualification import QualificationService
from inference.adapter_runtime import AdapterRuntime
from memory.context import ContextService
from memory.governance import MemoryGovernance
from memory.orchestrator import MemoryOrchestrator
from rag.answering import GroundedAnswering, answer_with_citations
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from release.governance import ReleaseGovernance
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase
from utils.auth import (
    create_access_token,
    decode_identity,
    get_current_identity,
    verify_password,
)
from utils.oidc import begin as begin_oidc
from utils.oidc import finish as finish_oidc
from utils.s3_utils import S3Utils
from utils.user_db import get_user, init_user_db

# S3/MinIO Configuration (Now imported from config.py)
FEEDBACK_S3_PREFIX = "feedback"


def get_s3_client():
    """Get configured S3 client for MinIO"""
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # 强制使用路径风格
        ),
        region_name="us-east-1",
    )


def _index_feedback_annotation(
    identity: dict[str, str], data: dict[str, Any], key: str, body: bytes
) -> str | None:
    """Index run-bound feedback once in the PostgreSQL H5 authority."""
    run_id = data.get("run_id")
    if not run_id:
        return None
    source_key = f"{key}.source"
    content_sha256 = hashlib.sha256(body).hexdigest()
    with PostgresDatabase(DATABASE_URL).transaction(identity) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT annotation_id FROM trajectory_annotations "
                "WHERE tenant_id = %s AND run_id = %s AND kind = 'user_feedback' "
                "AND content_key = %s LIMIT 1",
                (identity["tenant_id"], run_id, source_key),
            )
            row = cursor.fetchone()
    if row:
        return str(row["annotation_id"])
    get_s3_client().put_object(
        Bucket=MINIO_BUCKET,
        Key=source_key,
        Body=body,
        ContentType="application/json",
    )
    return EvaluationService(DATABASE_URL).create_annotation(
        identity,
        run_id=run_id,
        trial_id=None,
        kind="user_feedback",
        label={
            "feedback_id": data.get("feedback_id") or key.rsplit("/", 1)[-1],
            "feedback": data.get("feedback", "unrated"),
            "query": data.get("query", ""),
            "answer": data.get("answer", ""),
        },
        content_key=source_key,
        content_sha256=content_sha256,
    )


import subprocess
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # 1. Ensure certificates exist for HTTPS if possible (local or container)
    webui_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(webui_dir, "cert.pem")
    key_path = os.path.join(webui_dir, "key.pem")

    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        try:
            from webui.generate_cert import generate_self_signed_cert

            logger.info("Certificates not found. Generating self-signed certificates...")
            generate_self_signed_cert(cert_path, key_path)
        except Exception as e:
            logger.warning(f"Failed to generate certificates: {e}. HTTPS may not be available.")

    validate_config()
    init_user_db()
    yield
    # Shutdown
    logger.info("Shutting down and releasing resources...")
    try:
        if _adapter_runtime.batch_engine is not None:
            await _adapter_runtime.batch_engine.shutdown()
        _adapter_runtime.model_manager.clear_cache()
        coordinator.clear_agents()
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    finally:
        logger.info("Shutting down. Releasing GPU resources...")
        sys.stdout.flush()
        # On Linux/ROCm, a hard exit is sometimes needed to prevent driver hangs
        # but we'll try to allow a graceful exit first or use a shorter timeout if possible.
        # For now, we keep os._exit(0) as a fallback but allow normal return if possible.
        if os.getenv("FORCE_EXIT", "true").lower() == "true":
            os._exit(0)
        else:
            logger.info("Graceful exit requested.")


app = FastAPI(title="DataAlchemy WebUI", lifespan=lifespan)


@app.get("/metrics")
async def metrics():
    logger.info("Metrics endpoint hit")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Initialize Coordinator
# Note: We use 'python' mode by default for the WebUI
coordinator = Coordinator(mode="python")
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


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    # Token validation for WebSockets
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    identity = decode_identity(token)
    if not identity:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    username = identity["username"]
    tenant_id = identity["tenant_id"]

    logger.info(f"WebSocket connection accepted for user: {username}")
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_text()
            request_data = json.loads(data)
            query = request_data.get("query")

            if not query:
                await websocket.send_json({"error": "Query cannot be empty"})
                continue

            logger.info(f"WebSocket query from {username}: {query}")

            session_id = request_data.get("session_id")
            context_service = _context_service()

            if session_id:
                try:
                    session = context_service.get_session(session_id, identity)
                except PermissionError:
                    await websocket.send_json({"error": "Session not found"})
                    continue
                if session["state"] != "active":
                    await websocket.send_json({"error": "Session is not active"})
                    continue
            else:
                session = context_service.create_session(identity)
                session_id = session["session_id"]
            user_event = context_service.append_event(
                session_id, "user_message", {"content": query}, identity
            )
            envelope = context_service.build_context(session_id, query, identity)
            context = envelope["retrieval_context"]

            await websocket.send_json({"type": "status", "content": "Retrieving knowledge..."})

            await websocket.send_json({"type": "status", "content": "Consulting LoRA model..."})

            await websocket.send_json({"type": "status", "content": "Fusing response..."})
            final_answer, citations, model_execution = await answer_with_citations(
                query,
                identity,
                context,
                _adapter_runtime,
                _answering,
                cache_scope=_cache_scope(identity),
            )

            # Save feedback
            feedback_id = coordinator.save_feedback(
                query,
                final_answer,
                owner=username,
                tenant_id=tenant_id,
                run_id=request_data.get("run_id"),
            )

            context_service.append_event(
                session_id,
                "assistant_message",
                {
                    "content": final_answer,
                    "citations": citations,
                    "user_event_id": user_event["event_id"],
                },
                identity,
                trust_label="trusted_system",
            )

            # Send final answer
            await websocket.send_json(
                {
                    "type": "answer",
                    "content": final_answer,
                    "feedback_id": feedback_id,
                    "session_id": session_id,
                    "citations": citations,
                    "model_execution": model_execution,
                }
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    run_id: Optional[str] = None


class SessionCreate(BaseModel):
    title: Optional[str] = "New Chat"
    auto_memory_enabled: bool = False


class SessionPatch(BaseModel):
    auto_memory_enabled: Optional[bool] = None
    title: Optional[str] = None
    expected_version: int


class ChatResponse(BaseModel):
    answer: str
    feedback_id: str
    session_id: str
    run_id: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    model_execution: dict[str, Any] = Field(default_factory=dict)


class FeedbackUpdateRequest(BaseModel):
    feedback_id: str
    feedback: str  # "good" or "bad"


class FeedbackReviewRequest(BaseModel):
    feedback_id: str
    review_status: str
    training_allowed: bool = False
    training_purpose: Optional[str] = None
    permission_version: Optional[str] = None
    reason: Optional[str] = None


class H5AnnotationDecisionRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected|revoked)$")
    training_allowed: bool = False
    training_purpose: Optional[str] = None
    permission_version: Optional[str] = None
    reason: Optional[str] = None


class H5SnapshotDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|revoke)$")
    reason: Optional[str] = None


class H6QualificationCreateRequest(BaseModel):
    purpose: str = Field(min_length=1, max_length=200)
    source_manifest_key: str = Field(min_length=1, max_length=1024)
    source_manifest_sha256: str = Field(min_length=64, max_length=64)
    source_acl_digest: str = Field(min_length=1, max_length=256)
    permission_version: str = Field(min_length=1, max_length=200)
    data_classification: str = Field(min_length=1, max_length=100)
    suite_version: str = Field(min_length=1, max_length=200)
    suite_sha256: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(min_length=1, max_length=200)
    retention: dict[str, Any] = Field(default_factory=dict)
    allowed_processing: dict[str, Any] = Field(default_factory=dict)


class H6QualificationDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve_data|calibrate|pilot_ready|revoke)$")
    reason: Optional[str] = None
    base_evaluation_id: Optional[str] = None
    candidate_evaluation_id: Optional[str] = None
    calibration_report_key: Optional[str] = None
    calibration_report_sha256: Optional[str] = None
    stable_release_id: Optional[str] = None
    candidate_release_id: Optional[str] = None
    deployment_evidence_key: Optional[str] = None
    deployment_evidence_sha256: Optional[str] = None


class H6PilotEvidenceRequest(BaseModel):
    kind: str = Field(pattern="^(weekly_audit|incident|exception|team_signoff)$")
    artifact_key: str = Field(min_length=1, max_length=1024)
    artifact_sha256: str = Field(min_length=64, max_length=64)
    reviewer: str = Field(min_length=1, max_length=200)
    outcome: str = Field(pattern="^(passed|failed|open)$")
    week_no: Optional[int] = Field(default=None, ge=1, le=4)
    run_refs: list[str] = Field(default_factory=list)


class H6PilotCreateRequest(BaseModel):
    team_id: str = Field(min_length=1, max_length=200)
    qualification_id: str
    stable_release_id: str
    candidate_release_id: str
    owner: str = Field(min_length=1, max_length=200)
    security_contact: str = Field(min_length=1, max_length=200)
    policy: dict[str, Any] = Field(default_factory=dict)


class ReleaseAdvanceRequest(BaseModel):
    target: str = Field(pattern="^(shadow|canary|promoted|rolled_back|rejected)$")
    expected_version: Optional[int] = Field(default=None, ge=1)


class ReleaseObservationRequest(BaseModel):
    sample_count: int = Field(ge=0)
    window_seconds: int = Field(ge=0)
    security_passed: bool
    window_complete: bool
    error_rate: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    promote: bool = False


class MemoryCreateRequest(BaseModel):
    kind: str = Field(pattern="^(episodic|profile|procedural)$")
    content: str = Field(min_length=1, max_length=10_000)
    source_event_id: str


class MemoryApprovalRequest(BaseModel):
    approved: bool


class MemoryDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    expected_version: Optional[int] = None


class MemoryConflictResolveRequest(BaseModel):
    policy_version: str = "memory-policy.v1"


class MemoryRevisionRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    source_event_id: str


class ReloadResponse(BaseModel):
    status: str
    message: str
    model_execution: dict[str, Any] = Field(default_factory=dict)


class ReloadRequest(BaseModel):
    release_id: Optional[str] = None
    expected_adapter_id: Optional[str] = None
    expected_artifact_sha256: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str


class TaskCreateRequest(BaseModel):
    goal: str
    execution_mode: str = Field(default="legacy", pattern="^(legacy|strict)$")
    tool: Optional[str] = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    steps: Optional[list[dict[str, Any]]] = None
    success_criteria: Optional[list[dict[str, Any]]] = None
    data_scope: Optional[dict[str, Any]] = None
    limits: Optional[dict[str, Any]] = None
    max_steps: int = Field(default=8, ge=1, le=8)


class TaskApprovalRequest(BaseModel):
    approved: bool
    expected_version: Optional[int] = Field(default=None, ge=1)


class TaskControlRequest(BaseModel):
    expected_version: Optional[int] = Field(default=None, ge=1)


class TaskReplanRequest(BaseModel):
    remaining_steps: list[dict[str, Any]]
    reason: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


@app.post("/api/jobs/full-cycle")
async def trigger_full_cycle(identity: dict = Depends(get_current_identity)):
    """The annotation bypass is intentionally closed by the H2 harness."""
    _require_admin(identity)
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="full-cycle bypass is disabled; create a strict harness task instead",
    )


@app.get("/api/models/status")
async def model_status(identity: dict = Depends(get_current_identity)):
    """Return tenant-scoped active model evidence."""
    _require_admin(identity)
    try:
        return _adapter_runtime.model_status(identity)
    except (PermissionError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/models/reload", response_model=ReloadResponse)
async def reload_model(
    request: ReloadRequest | None = None, identity: dict = Depends(get_current_identity)
):
    """Load one explicitly selected, tenant-scoped promoted release."""
    _require_admin(identity)
    request = request or ReloadRequest()
    try:
        # Run in executor as it might involve S3 downloads and model loading
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            _adapter_runtime.check_and_reload_adapter,
            True,
            identity,
            request.release_id,
        )
        status = _adapter_runtime.model_status(identity)
        if request.expected_adapter_id and status.get("adapter_id") != request.expected_adapter_id:
            raise HTTPException(status_code=409, detail="active_adapter_mismatch")
        if (
            request.expected_artifact_sha256
            and status.get("adapter_artifact_sha256") != request.expected_artifact_sha256
        ):
            raise HTTPException(status_code=409, detail="active_adapter_hash_mismatch")

        if success:
            return {
                "status": "succeeded",
                "message": "Selected model release loaded.",
                "model_execution": status,
            }
        if request.release_id and status.get("release_id") == request.release_id:
            return {
                "status": "already_current",
                "message": "Selected release is already active.",
                "model_execution": status,
            }
        return {
            "status": "failed",
            "message": "Selected release was not activated.",
            "model_execution": status,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reloading model: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/auth/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    if AUTH_MODE != "local":
        raise HTTPException(status_code=404, detail="Local login is disabled")
    user = get_user(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"], "tenant_id": user["tenant_id"], "role": user["role"]},
        expires_delta=access_token_expires,
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/auth/oidc/login")
async def oidc_login():
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=404, detail="OIDC login is disabled")
    authorization_url, _ = begin_oidc()
    return RedirectResponse(authorization_url)


@app.get("/api/auth/oidc/callback", response_model=Token)
async def oidc_callback(code: str, state: str):
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=404, detail="OIDC login is disabled")
    try:
        identity = finish_oidc(code, state)
    except PermissionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    access_token = create_access_token({"sub": identity["username"], **identity})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/auth/me")
async def read_users_me(identity: dict = Depends(get_current_identity)):
    return identity


@app.get("/api/audit-events")
async def list_audit_events(identity: dict = Depends(get_current_identity)):
    _require_admin(identity)
    try:
        return {"events": audit_log.list(identity)}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@app.get("/api/sessions")
async def list_sessions(identity: dict = Depends(get_current_identity)):
    sessions = _context_service().list_sessions(identity)
    logger.info("API: Found %s durable sessions for user %s", len(sessions), identity["username"])
    return {"sessions": sessions, "authority": "postgresql"}


@app.post("/api/sessions")
async def create_session(request: SessionCreate, identity: dict = Depends(get_current_identity)):
    session = _context_service().create_session(
        identity, request.title or "New Chat", request.auto_memory_enabled
    )
    return {
        "session_id": session["session_id"],
        "version": session["version"],
        "authority": "postgresql",
    }


@app.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        session = _context_service().get_session(session_id, identity)
        messages = _context_service().events(session_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return {"session": session, "messages": messages, "authority": "postgresql"}


@app.patch("/api/sessions/{session_id}")
async def patch_session(
    session_id: str, request: SessionPatch, identity: dict = Depends(get_current_identity)
):
    if request.auto_memory_enabled is None:
        return _context_service().get_session(session_id, identity)
    try:
        return _context_service().set_auto_memory(
            session_id, request.auto_memory_enabled, identity, request.expected_version
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/sessions/{session_id}/context")
async def get_session_context(
    session_id: str, query: str = "", identity: dict = Depends(get_current_identity)
):
    try:
        envelope = _context_service().build_context(session_id, query or "", identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return {
        key: envelope[key]
        for key in (
            "snapshot_id",
            "task",
            "packs",
            "handoff",
            "recent_event_ids",
            "document_chunk_ids",
            "memory_ids",
            "budget",
            "envelope_sha256",
        )
    }


@app.post("/api/sessions/{session_id}/close")
async def close_session(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        service = _context_service()
        checkpoint = service.compact(session_id, identity)
        service.append_event(
            session_id, "session_closed", {"checkpoint_id": checkpoint["checkpoint_id"]}, identity
        )
        with service.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversation_sessions SET state = 'closed', closed_at = now(), version = version + 1, updated_at = now() "
                    "WHERE session_id = %s AND owner_id = %s AND state = 'active'",
                    (session_id, identity["username"]),
                )
        distillation = _distill_session(session_id, identity, service)
        return {
            "session_id": session_id,
            "state": "closed",
            "checkpoint": checkpoint,
            "distillation": distillation,
        }
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


def _distill_session(
    session_id: str, identity: dict[str, str], service: ContextService | None = None
) -> dict[str, Any]:
    service = service or _context_service()
    session = service.get_session(session_id, identity)
    candidates = service.extract_candidates(service.events(session_id, identity))
    orchestrator = _memory_orchestrator()
    results = []
    for candidate in candidates:
        try:
            results.append(
                orchestrator.create_governed_candidate(
                    identity, candidate, auto_memory_enabled=session["auto_memory_enabled"]
                )
            )
        except (PermissionError, ValueError) as error:
            results.append({"status": "rejected", "reason": str(error)})
    return {"candidate_count": len(results), "decisions": results}


@app.post("/api/sessions/{session_id}/distill")
async def distill_session(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return _distill_session(session_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


@app.post("/api/sessions/{session_id}/reset")
async def reset_session(
    session_id: str, expected_version: int, identity: dict = Depends(get_current_identity)
):
    try:
        return _context_service().reset(session_id, identity, expected_version)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


@app.post("/api/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    task_spec_sha256: Optional[str] = None,
    plan_version: Optional[int] = None,
    identity: dict = Depends(get_current_identity),
):
    try:
        return _context_service().resume(
            session_id,
            identity,
            task_spec_sha256=task_spec_sha256,
            plan_version=plan_version,
        )
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/history")
async def get_history(identity: dict = Depends(get_current_identity)):
    # Legacy shape, backed by the durable session store during migration.
    service = _context_service()
    history = []
    for session in service.list_sessions(identity):
        history.extend(service.events(session["session_id"], identity))
    return {"history": history, "deprecated": True, "authority": "postgresql"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, identity: dict = Depends(get_current_identity)):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    try:
        context_service = _context_service()
        session_id = request.session_id
        if session_id:
            try:
                session = context_service.get_session(session_id, identity)
            except PermissionError as error:
                raise HTTPException(status_code=404, detail="Session not found") from error
            if session["state"] != "active":
                raise HTTPException(status_code=409, detail="Session is not active")
        else:
            session = context_service.create_session(identity)
            session_id = session["session_id"]

        task_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        user_event = context_service.append_event(
            session_id,
            "user_message",
            {"content": request.query},
            identity,
            task_id=task_id,
            run_id=run_id,
        )
        envelope = context_service.build_context(
            session_id,
            request.query,
            identity,
            task_type="rag_chat",
            task={"task_id": task_id, "run_id": run_id, "plan_version": 1},
        )
        context_ref, context_object_sha256 = _publish_chat_context(envelope, identity, run_id)
        document_ids = sorted(
            {
                str(item["document_id"])
                for item in envelope["retrieval_context"]
                if item.get("document_id")
            }
        )
        task = agent_runtime.create_task(
            identity,
            "Tenant-scoped RAG chat",
            [
                {
                    "tool": "rag_chat",
                    "arguments": {
                        "context_ref": context_ref,
                        "context_sha256": context_object_sha256,
                    },
                    "scope_refs": [context_ref],
                    "verifier_refs": ["chat-capture"],
                }
            ],
            max_steps=1,
            execution_mode="strict",
            task_spec={
                "success_criteria": [
                    {
                        "criterion_id": "chat-capture",
                        "verifier": "verify_chat_capture",
                        "version": 1,
                        "parameters": {
                            "snapshot_id": envelope["snapshot_id"],
                            "context_sha256": envelope["envelope_sha256"],
                            "document_ids": document_ids,
                        },
                        "phase": "after_step",
                        "required": True,
                    }
                ],
                "data_scope": {"source_refs": [context_ref]},
                "limits": {"max_steps": 1, "deadline_seconds": 300},
            },
            task_id=task_id,
            run_id=run_id,
        )
        completed = await agent_runtime.run(task["task_id"], identity)
        tool_runs = agent_runtime.tool_runs(task["task_id"], identity)
        output = tool_runs[-1]["result"]["output"] if tool_runs else {}
        response_ref = output.get("response_ref")
        response_sha256 = output.get("response_sha256")
        response_body = _evidence_store.get(response_ref) if isinstance(response_ref, str) else None
        if (
            response_body is None
            or sha256(response_body) != response_sha256
            or completed["state"] != "succeeded"
        ):
            raise RuntimeError(
                f"rag_chat_run_failed:{completed['state']}:{completed.get('finish_reason')}"
            )
        response = json.loads(response_body)
        answer = response["answer"]
        citations = response["citations"]
        model_execution = response["model_execution"]

        context_service.append_event(
            session_id,
            "assistant_message",
            {"content": answer, "citations": citations, "user_event_id": user_event["event_id"]},
            identity,
            trust_label="trusted_system",
            task_id=task_id,
            run_id=run_id,
        )

        # Save feedback record (file-based)
        feedback_id = coordinator.save_feedback(
            request.query,
            answer,
            owner=identity["username"],
            tenant_id=identity["tenant_id"],
            run_id=run_id,
        )
        return ChatResponse(
            answer=answer,
            feedback_id=feedback_id,
            session_id=session_id,
            run_id=run_id,
            citations=citations,
            model_execution=model_execution,
        )
    except Exception as e:
        logger.error(f"Error during chat: {e}", exc_info=True)
        if isinstance(e, HTTPException):
            raise
        if isinstance(e, PermissionError):
            raise HTTPException(status_code=403, detail=str(e)) from e
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/pilot-runs/document")
async def create_document_pilot_run(
    file: UploadFile = File(...),
    question: str = Form(...),
    acl: str = Form(""),
    expected_phrase: str = Form(""),
    identity: dict = Depends(get_current_identity),
):
    """Land one PDF/DOCX and create its durable H3 strict task."""
    _require_admin(identity)
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    try:
        body = await file.read()
        safe_name, content_type = validate_upload(file.filename or "", body, file.content_type)
        readers = (
            json.loads(acl)
            if acl.strip()
            else [
                {"subject_type": "user", "subject_id": identity["username"], "permission": "read"}
            ]
        )
        if not isinstance(readers, list) or not readers:
            raise DocumentRejected("acl_empty")
        for reader in readers:
            if (
                not isinstance(reader, dict)
                or reader.get("subject_type") not in {"user", "role", "tenant"}
                or not isinstance(reader.get("subject_id"), str)
                or not reader["subject_id"].strip()
            ):
                raise DocumentRejected("acl_invalid")
        input_id = str(uuid.uuid4())
        raw_prefix = f"raw/harness/{identity['tenant_id']}/{input_id}"
        raw_key = f"{raw_prefix}/documents/{safe_name}"
        descriptor_key = f"{raw_prefix}/input.json"
        source_uri = f"s3://{MINIO_BUCKET}/{raw_key}"
        descriptor = build_input_descriptor(
            input_id=input_id,
            tenant_id=identity["tenant_id"],
            source_uri=source_uri,
            filename=safe_name,
            content_type=content_type,
            body=body,
            acl=readers,
            owner=identity["username"],
        )
        descriptor["source"]["object_key"] = raw_key
        store = S3Utils()
        if not store.put_object(raw_key, body, content_type):
            raise RuntimeError("raw_upload_failed")
        descriptor_bytes = json.dumps(descriptor, ensure_ascii=False, sort_keys=True).encode()
        if not store.put_object(descriptor_key, descriptor_bytes, "application/json"):
            raise RuntimeError("input_manifest_upload_failed")

        descriptor_ref = f"raw:{descriptor_key}"
        raw_ref = f"raw:s3a://{MINIO_BUCKET}/{raw_prefix}"
        postgres_ref = f"postgres:tenant:{identity['tenant_id']}"
        criteria = [
            {
                "criterion_id": "input",
                "verifier": "verify_input_manifest",
                "version": 1,
                "parameters": {},
                "phase": "after_step",
                "required": True,
            },
            {
                "criterion_id": "rough",
                "verifier": "verify_rough_clean",
                "version": 2,
                "parameters": {},
                "phase": "after_step",
                "required": True,
            },
            {
                "criterion_id": "refine",
                "verifier": "verify_refined_corpus",
                "version": 1,
                "parameters": {},
                "phase": "after_step",
                "required": True,
            },
            {
                "criterion_id": "publish",
                "verifier": "verify_ingest",
                "version": 2,
                "parameters": {"expected_phrase": expected_phrase},
                "phase": "after_step",
                "required": True,
            },
            {
                "criterion_id": "retrieval",
                "verifier": "verify_retrieval",
                "version": 2,
                "parameters": {"query": question},
                "phase": "after_step",
                "required": True,
            },
        ]
        plan = [
            {
                "tool": "validate_document_input",
                "arguments": {"input_key": descriptor_key, "input_sha256": sha256_bytes(body)},
                "scope_refs": [descriptor_ref],
                "verifier_refs": ["input"],
            },
            {
                "tool": "spark_rough_clean",
                "arguments": {
                    "input_key": f"s3a://{MINIO_BUCKET}/{raw_prefix}",
                    "input_sha256": sha256_bytes(body),
                },
                "scope_refs": [raw_ref],
                "verifier_refs": ["rough"],
            },
            {
                "tool": "refine_corpus",
                "arguments": {"input_key": descriptor_key},
                "scope_refs": [descriptor_ref],
                "verifier_refs": ["refine"],
            },
            {
                "tool": "publish_corpus",
                "arguments": {"input_key": descriptor_key},
                "scope_refs": [descriptor_ref, postgres_ref],
                "verifier_refs": ["publish"],
            },
            {
                "tool": "rag_probe",
                "arguments": {"query": question},
                "scope_refs": [postgres_ref],
                "verifier_refs": ["retrieval"],
            },
        ]
        task = agent_runtime.create_task(
            identity,
            f"Process and answer from {safe_name}",
            plan,
            max_steps=5,
            execution_mode="strict",
            task_spec={
                "success_criteria": criteria,
                "data_scope": {"source_refs": [descriptor_ref, raw_ref, postgres_ref]},
                "limits": {"max_steps": 5, "deadline_seconds": 3600},
            },
        )
        task = await agent_runtime.run(task["task_id"], identity)
        return {
            "run_id": task["run_id"],
            "task_id": task["task_id"],
            "input": descriptor,
            "task": task,
        }
    except (
        DocumentRejected,
        json.JSONDecodeError,
        KeyError,
        PermissionError,
        RuntimeError,
        ValueError,
    ) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _task_http_error(error: Exception) -> HTTPException:
    if isinstance(error, (KeyError, PermissionError)):
        return HTTPException(status_code=404, detail="Task not found")
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/tasks")
async def create_task(request: TaskCreateRequest, identity: dict = Depends(get_current_identity)):
    """Create and execute a durable legacy or strict task contract."""
    try:
        strict = request.execution_mode == "strict"
        if strict:
            if request.tool is not None or request.steps is None:
                raise ValueError("Strict tasks require steps and cannot include tool")
            if (
                request.success_criteria is None
                or request.data_scope is None
                or request.limits is None
            ):
                raise ValueError("Strict tasks require success_criteria, data_scope, and limits")
            plan = request.steps
            task_spec = {
                "success_criteria": request.success_criteria,
                "data_scope": request.data_scope,
                "limits": request.limits,
            }
            max_steps = request.limits.get("max_steps", request.max_steps)
        else:
            if request.steps is not None:
                raise ValueError("Legacy tasks use tool and arguments, not steps")
            plan = [{"tool": request.tool or "rag_chat", "arguments": request.arguments}]
            task_spec = None
            max_steps = request.max_steps
        task = agent_runtime.create_task(
            identity,
            request.goal,
            plan,
            max_steps,
            execution_mode=request.execution_mode,
            task_spec=task_spec,
        )
        return await agent_runtime.run(task["task_id"], identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.get("/api/tasks")
async def list_tasks(identity: dict = Depends(get_current_identity)):
    return {"tasks": agent_runtime.list_tasks(identity)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return agent_runtime.get_task(task_id, identity)
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@app.get("/api/tasks/{task_id}/events")
async def get_task_events(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return {"events": agent_runtime.events(task_id, identity)}
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@app.get("/api/tasks/{task_id}/verifications")
async def get_task_verifications(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return {"verifications": agent_runtime.verifications(task_id, identity)}
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, identity: dict = Depends(get_current_identity)):
    try:
        task = next(task for task in agent_runtime.list_tasks(identity) if task["run_id"] == run_id)
        return {
            "task": task,
            "evidence": agent_runtime.evidence_status(task["task_id"], identity),
            **_run_details(task, identity),
        }
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/api/runs/{run_id}/manifest")
async def get_run_manifest(run_id: str, identity: dict = Depends(get_current_identity)):
    if agent_runtime.evidence is None:
        raise HTTPException(status_code=503, detail="Evidence publishing is not configured")
    try:
        return agent_runtime.evidence.manifest(run_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Published manifest not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/runs/{run_id}/reconcile")
async def reconcile_run(
    run_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    _require_admin(identity)
    try:
        task = next(task for task in agent_runtime.list_tasks(identity) if task["run_id"] == run_id)
        if task["state"] in {"waiting_job", "cancelling"}:
            return await agent_runtime.reconcile_job(
                task["task_id"], identity, request.expected_version
            )
        return agent_runtime.reconcile_evidence(task["task_id"], identity, request.expected_version)
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.delete("/api/runs/{run_id}/manifest")
async def delete_run_manifest(run_id: str, identity: dict = Depends(get_current_identity)):
    _require_admin(identity)
    try:
        task = next(task for task in agent_runtime.list_tasks(identity) if task["run_id"] == run_id)
        agent_runtime.delete_evidence(task["task_id"], identity)
        return {"status": "deleted", "run_id": run_id}
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return agent_runtime.pause(task_id, identity, request.expected_version)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        agent_runtime.resume(task_id, identity, request.expected_version)
        return await agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        agent_runtime.retry(task_id, identity, request.expected_version)
        return await agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/retry-verification")
async def retry_task_verification(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        agent_runtime.retry_verification(task_id, identity, request.expected_version)
        return await agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/approval")
async def approve_task(
    task_id: str,
    request: TaskApprovalRequest,
    identity: dict = Depends(get_current_identity),
):
    try:
        task = agent_runtime.approve(task_id, identity, request.approved, request.expected_version)
        return await agent_runtime.run(task_id, identity) if request.approved else task
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return agent_runtime.cancel(task_id, identity, request.expected_version)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/replan")
async def replan_task(
    task_id: str, request: TaskReplanRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return agent_runtime.replan(
            task_id, identity, request.remaining_steps, request.reason, request.expected_version
        )
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/feedback")
async def update_feedback(
    request: FeedbackUpdateRequest, identity: dict = Depends(get_current_identity)
):
    """Update feedback status in S3."""
    if request.feedback not in ["good", "bad"]:
        raise HTTPException(status_code=400, detail="Invalid feedback value")

    try:
        s3 = get_s3_client()
        s3_key = f"{FEEDBACK_S3_PREFIX}/{request.feedback_id}"

        # 1. Download existing
        response = s3.get_object(Bucket=MINIO_BUCKET, Key=s3_key)
        data = json.loads(response["Body"].read().decode("utf-8"))

        if (
            data.get("owner") != identity["username"]
            or data.get("tenant_id") != identity["tenant_id"]
        ):
            raise HTTPException(status_code=404, detail="Feedback not found")

        # 2. Update
        data["feedback"] = request.feedback
        data["updated_at"] = datetime.datetime.now().isoformat()
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        # 3. Upload back
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=body,
            ContentType="application/json",
        )
        annotation_id = _index_feedback_annotation(identity, data, s3_key, body)

        logger.info(f"Feedback updated in S3 for {request.feedback_id} to {request.feedback}")
        return {"status": "success", "annotation_id": annotation_id}
    except Exception as e:
        logger.error(f"Error updating feedback in S3: {e}")
        raise HTTPException(status_code=500, detail=f"S3 Update failed: {str(e)}") from e


@app.post("/api/feedback/review")
async def review_feedback(
    request: FeedbackReviewRequest, identity: dict = Depends(get_current_identity)
):
    """Approve or reject a feedback record before it can become training data."""
    _require_reviewer(identity)
    if request.review_status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Invalid review status")

    try:
        s3 = get_s3_client()
        s3_key = f"{FEEDBACK_S3_PREFIX}/{request.feedback_id}"
        response = s3.get_object(Bucket=MINIO_BUCKET, Key=s3_key)
        data = json.loads(response["Body"].read().decode("utf-8"))
        if data.get("tenant_id") != identity["tenant_id"]:
            raise HTTPException(status_code=404, detail="Feedback not found")

        data["review_status"] = request.review_status
        data["reviewed_by"] = identity["username"]
        data["reviewed_at"] = datetime.datetime.now().isoformat()
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=body,
            ContentType="application/json",
        )
        annotation_id = _index_feedback_annotation(identity, data, s3_key, body)
        if annotation_id:
            EvaluationService(DATABASE_URL).review_annotation(
                identity,
                annotation_id,
                status=request.review_status,
                training_allowed=request.training_allowed,
                training_purpose=request.training_purpose,
                permission_version=request.permission_version,
                reason=request.reason,
            )
        return {"status": "success", "annotation_id": annotation_id}
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.error(f"Error reviewing feedback: {error}")
        raise HTTPException(status_code=500, detail="Feedback review failed") from error


@app.post("/api/annotations/{annotation_id}/decision")
@app.post("/api/h5/annotations/{annotation_id}/decision")
async def decide_h5_annotation(
    annotation_id: str,
    request: H5AnnotationDecisionRequest,
    identity: dict = Depends(get_current_identity),
):
    _require_reviewer(identity)
    try:
        EvaluationService(DATABASE_URL).review_annotation(
            identity,
            annotation_id,
            status=request.status,
            training_allowed=request.training_allowed,
            training_purpose=request.training_purpose,
            permission_version=request.permission_version,
            reason=request.reason,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"annotation_id": annotation_id, "status": request.status}


@app.post("/api/qualifications")
@app.post("/api/h6/qualifications")
async def create_h6_qualification(
    request: H6QualificationCreateRequest,
    identity: dict = Depends(get_current_identity),
):
    try:
        qualification_id = QualificationService(DATABASE_URL).create(
            identity,
            purpose=request.purpose,
            source_manifest_key=request.source_manifest_key,
            source_manifest_sha256=request.source_manifest_sha256,
            source_acl_digest=request.source_acl_digest,
            permission_version=request.permission_version,
            data_classification=request.data_classification,
            suite_version=request.suite_version,
            suite_sha256=request.suite_sha256,
            policy_version=request.policy_version,
            retention=request.retention,
            allowed_processing=request.allowed_processing,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"qualification_id": qualification_id, "state": "draft"}


@app.get("/api/qualifications")
@app.get("/api/h6/qualifications")
async def list_h6_qualifications(identity: dict = Depends(get_current_identity)):
    return {"qualifications": QualificationService(DATABASE_URL).list(identity)}


@app.get("/api/qualifications/{qualification_id}")
@app.get("/api/h6/qualifications/{qualification_id}")
async def get_h6_qualification(
    qualification_id: str, identity: dict = Depends(get_current_identity)
):
    qualification = QualificationService(DATABASE_URL).get(identity, qualification_id)
    if qualification is None:
        raise HTTPException(status_code=404, detail="Qualification not found")
    return qualification


@app.post("/api/qualifications/{qualification_id}/decision")
@app.post("/api/h6/qualifications/{qualification_id}/decision")
async def decide_h6_qualification(
    qualification_id: str,
    request: H6QualificationDecisionRequest,
    identity: dict = Depends(get_current_identity),
):
    service = QualificationService(DATABASE_URL)
    try:
        if request.decision == "approve_data":
            service.approve_data(identity, qualification_id)
        elif request.decision == "calibrate":
            required = (
                request.base_evaluation_id,
                request.candidate_evaluation_id,
                request.calibration_report_key,
                request.calibration_report_sha256,
            )
            if any(value is None for value in required):
                raise ValueError("calibration_fields_missing")
            service.mark_calibrated(
                identity,
                qualification_id,
                base_evaluation_id=request.base_evaluation_id,
                candidate_evaluation_id=request.candidate_evaluation_id,
                calibration_report_key=request.calibration_report_key,
                calibration_report_sha256=request.calibration_report_sha256,
            )
        elif request.decision == "pilot_ready":
            required = (
                request.stable_release_id,
                request.candidate_release_id,
                request.deployment_evidence_key,
                request.deployment_evidence_sha256,
            )
            if any(value is None for value in required):
                raise ValueError("deployment_fields_missing")
            service.mark_pilot_ready(
                identity,
                qualification_id,
                stable_release_id=request.stable_release_id,
                candidate_release_id=request.candidate_release_id,
                deployment_evidence_key=request.deployment_evidence_key,
                deployment_evidence_sha256=request.deployment_evidence_sha256,
            )
        else:
            service.revoke(identity, qualification_id, request.reason or "reviewer_revoked")
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    qualification = service.get(identity, qualification_id)
    return {
        "qualification_id": qualification_id,
        "state": qualification["state"] if qualification else "revoked",
    }


@app.post("/api/h6/pilots")
async def create_h6_pilot(
    request: H6PilotCreateRequest, identity: dict = Depends(get_current_identity)
):
    try:
        pilot_id = PilotService(DATABASE_URL).create(
            identity,
            team_id=request.team_id,
            qualification_id=request.qualification_id,
            stable_release_id=request.stable_release_id,
            candidate_release_id=request.candidate_release_id,
            owner=request.owner,
            security_contact=request.security_contact,
            policy=request.policy,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"pilot_id": pilot_id, "state": "draft"}


@app.post("/api/h6/pilots/{pilot_id}/evidence")
async def record_h6_pilot_evidence(
    pilot_id: str, request: H6PilotEvidenceRequest, identity: dict = Depends(get_current_identity)
):
    try:
        evidence_id = PilotService(DATABASE_URL).record_evidence(
            identity,
            pilot_id,
            kind=request.kind,
            artifact_key=request.artifact_key,
            artifact_sha256=request.artifact_sha256,
            reviewer=request.reviewer,
            outcome=request.outcome,
            week_no=request.week_no,
            run_refs=request.run_refs,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"evidence_id": evidence_id}


@app.get("/api/h6/pilots/{pilot_id}")
async def get_h6_pilot(pilot_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return PilotService(DATABASE_URL).status(identity, pilot_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/evaluations/{evaluation_id}")
@app.get("/api/h5/evaluations/{evaluation_id}")
async def get_h5_evaluation(evaluation_id: str, identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM evaluation_campaigns WHERE evaluation_id = %s",
                (evaluation_id,),
            )
            campaign = cursor.fetchone()
            if campaign is None:
                raise HTTPException(status_code=404, detail="Evaluation not found")
            cursor.execute(
                "SELECT * FROM trajectory_trials WHERE evaluation_id = %s ORDER BY case_id, trial_no",
                (evaluation_id,),
            )
            trials = cursor.fetchall()
    return {
        "evaluation": {**campaign, "evaluation_id": str(campaign["evaluation_id"])},
        "trials": [{**trial, "trial_id": str(trial["trial_id"])} for trial in trials],
    }


@app.get("/api/annotations")
@app.get("/api/h5/annotations")
async def list_h5_annotations(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT annotation_id, trial_id, run_id, kind, label_json, source_acl_digest, "
                "training_allowed, training_purpose, training_permission_version, reviewer, status, "
                "reason, created_at, reviewed_at FROM trajectory_annotations "
                "ORDER BY created_at DESC LIMIT 200"
            )
            rows = cursor.fetchall()
    return {
        "annotations": [
            {
                **row,
                "annotation_id": str(row["annotation_id"]),
                "trial_id": str(row["trial_id"]) if row["trial_id"] else None,
                "run_id": str(row["run_id"]),
            }
            for row in rows
        ]
    }


@app.get("/api/training-snapshots")
@app.get("/api/h5/training-snapshots")
async def list_h5_snapshots(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT snapshot_id, state, dataset_key, dataset_sha256, dataset_size, policy_version, "
                "base_model_digest, created_by, approved_by, approved_at, revoke_reason, created_at "
                "FROM training_snapshots ORDER BY created_at DESC LIMIT 100"
            )
            rows = cursor.fetchall()
    return {"snapshots": [{**row, "snapshot_id": str(row["snapshot_id"])} for row in rows]}


@app.post("/api/training-snapshots/{snapshot_id}/decision")
@app.post("/api/h5/training-snapshots/{snapshot_id}/decision")
async def decide_h5_snapshot(
    snapshot_id: str,
    request: H5SnapshotDecisionRequest,
    identity: dict = Depends(get_current_identity),
):
    _require_reviewer(identity)
    service = EvaluationService(DATABASE_URL)
    try:
        if request.decision == "approve":
            service.approve_snapshot(identity, snapshot_id)
            result = "approved"
        else:
            service.revoke_snapshot(identity, snapshot_id, request.reason or "reviewer_revoked")
            result = "revoked"
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"snapshot_id": snapshot_id, "status": result}


@app.get("/api/adapters")
@app.get("/api/h5/adapters")
async def list_h5_adapters(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT adapter_id, snapshot_id, base_model_digest, tokenizer_digest, artifact_key, "
                "artifact_sha256, artifact_size, evaluation_id, state, safety_scan_json, created_at, "
                "revoked_at, revoke_reason FROM adapter_manifests ORDER BY created_at DESC LIMIT 100"
            )
            rows = cursor.fetchall()
    return {
        "adapters": [
            {**row, "adapter_id": str(row["adapter_id"]), "snapshot_id": str(row["snapshot_id"])}
            for row in rows
        ]
    }


@app.get("/api/releases")
@app.get("/api/h5/releases")
async def list_h5_releases(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT release_id, status, release_scope, adapter_id, evaluation_id, "
                "training_snapshot_id, rollback_release_id, approved_by, version, manifest_sha256, "
                "created_at, updated_at FROM release_records "
                "WHERE release_scope = 'single_tenant_lora' ORDER BY updated_at DESC LIMIT 100"
            )
            rows = cursor.fetchall()
    return {
        "releases": [
            {
                **row,
                "release_id": str(row["release_id"]),
                "adapter_id": str(row["adapter_id"]) if row["adapter_id"] else None,
                "evaluation_id": str(row["evaluation_id"]) if row["evaluation_id"] else None,
                "training_snapshot_id": str(row["training_snapshot_id"])
                if row["training_snapshot_id"]
                else None,
                "rollback_release_id": str(row["rollback_release_id"])
                if row["rollback_release_id"]
                else None,
            }
            for row in rows
        ]
    }


@app.post("/api/h5/releases/{release_id}/advance")
async def advance_h5_release(
    release_id: str,
    request: ReleaseAdvanceRequest,
    identity: dict = Depends(get_current_identity),
):
    _require_admin(identity)
    try:
        result = ReleaseGovernance(DATABASE_URL).advance(
            release_id, request.target, identity, request.expected_version
        )
    except (PermissionError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "release_id": release_id,
        "status": result.get("status"),
        "version": result.get("version"),
    }


@app.post("/api/h5/releases/{release_id}/observe")
async def observe_h5_release(
    release_id: str,
    request: ReleaseObservationRequest,
    identity: dict = Depends(get_current_identity),
):
    _require_admin(identity)
    try:
        status_value = ReleaseGovernance(DATABASE_URL).observe(
            release_id, request.model_dump(exclude={"promote"}), identity, promote=request.promote
        )
    except (PermissionError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"release_id": release_id, "status": status_value}


def _memory_orchestrator():
    return _memory


@app.get("/api/connectors/runs")
async def list_connector_runs(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id, connector_id, state, cursor_before, cursor_after, "
                "error_summary, started_at, completed_at FROM connector_runs "
                "ORDER BY started_at DESC LIMIT 50"
            )
            return {"runs": [{**row, "run_id": str(row["run_id"])} for row in cursor.fetchall()]}


@app.get("/api/memories")
async def list_memories(query: str, identity: dict = Depends(get_current_identity)):
    orchestrator = _memory_orchestrator()
    return {
        "memories": orchestrator.retrieve(query, identity)
        if query.strip()
        else orchestrator.list(identity),
        "authority": "postgresql",
    }


@app.post("/api/memories/preview")
async def preview_memory(
    request: MemoryCreateRequest, identity: dict = Depends(get_current_identity)
):
    service = _context_service()
    try:
        source = service.event(request.source_event_id, identity)
    except Exception:
        source = None
    return {
        "kind": request.kind,
        "content": request.content,
        "source_event_id": request.source_event_id,
        "status": "candidate",
        "policy": "approval_required",
        "source_visible": source is not None,
    }


@app.post("/api/memories")
async def create_memory(
    request: MemoryCreateRequest, identity: dict = Depends(get_current_identity)
):
    try:
        result = _memory_orchestrator().create_governed_candidate(
            identity,
            {
                "kind": request.kind,
                "content": request.content,
                "scope_type": "personal",
                "scope_id": identity["username"],
                "claim_key": f"manual.{request.kind}.{hashlib.sha256(request.content.encode()).hexdigest()[:16]}",
                "source_event_ids": [request.source_event_id],
                "confidence": 1.0,
                "trust_label": "trusted_user",
                "sensitivity_label": "none",
                "risk_class": "low",
            },
            auto_memory_enabled=False,
        )
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result


@app.post("/api/memories/{memory_id}/approval")
async def approve_memory(
    memory_id: str, request: MemoryApprovalRequest, identity: dict = Depends(get_current_identity)
):
    try:
        if not request.approved:
            _memory_orchestrator().reject(memory_id, identity)
            return {"memory_id": memory_id, "status": "rejected"}
        _require_admin(identity)
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        _memory_orchestrator().approve(memory_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"memory_id": memory_id, "status": "approved"}


@app.post("/api/memories/{memory_id}/decision")
async def decide_memory(
    memory_id: str, request: MemoryDecisionRequest, identity: dict = Depends(get_current_identity)
):
    try:
        if request.decision == "approve":
            _require_admin(identity)
            _memory_orchestrator().approve(memory_id, identity)
            status_value = "approved"
        else:
            _memory_orchestrator().reject(memory_id, identity)
            status_value = "rejected"
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"memory_id": memory_id, "status": status_value}


@app.post("/api/memories/{memory_id}/resolve-conflict")
async def resolve_memory_conflict(
    memory_id: str,
    request: MemoryConflictResolveRequest,
    identity: dict = Depends(get_current_identity),
):
    _require_admin(identity)
    try:
        MemoryGovernance(DATABASE_URL).resolve_conflict(memory_id, identity, request.policy_version)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"memory_id": memory_id, "status": "approved", "policy_version": request.policy_version}


@app.put("/api/memories/{memory_id}")
async def revise_memory(
    memory_id: str,
    request: MemoryRevisionRequest,
    identity: dict = Depends(get_current_identity),
):
    try:
        replacement_id = _memory_orchestrator().revise(
            memory_id, request.content, request.source_event_id, identity
        )
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"memory_id": replacement_id, "supersedes": memory_id, "status": "candidate"}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: str, identity: dict = Depends(get_current_identity)):
    try:
        request_id = _memory_orchestrator().delete("memory", memory_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Memory not found") from error
    return {"request_id": request_id, "status": "completed"}


# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

from webui.generate_cert import generate_self_signed_cert

if __name__ == "__main__":
    webui_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(webui_dir, "cert.pem")
    key_path = os.path.join(webui_dir, "key.pem")

    # Construct uvicorn command
    # Default to 8000, but allow override for local dev
    port = os.getenv("WEBUI_LISTEN_PORT", "8000")
    use_ssl = os.getenv("WEBUI_SSL", "false").lower() == "true"

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "webui.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--log-level",
        "info",
    ]

    if use_ssl:
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            print("[WebUI] SSL enabled but certificates not found. Generating...")
            generate_self_signed_cert(cert_path, key_path)

        if os.path.exists(cert_path) and os.path.exists(key_path):
            print(f"[WebUI] Starting HTTPS server on https://localhost:{port}")
            cmd.extend(["--ssl-keyfile", key_path, "--ssl-certfile", cert_path])
        else:
            print(
                "[WebUI] SSL enabled but certificates could not be generated. Falling back to HTTP."
            )
    else:
        print(f"[WebUI] Starting HTTP server on http://localhost:{port}")

    # Start server as a subprocess
    # This isolates the ROCm/PyTorch process from the launcher
    print("[WebUI] Launching server process...")
    process = subprocess.Popen(cmd, cwd=os.path.join(webui_dir, ".."))

    print(f"[WebUI] Server PID: {process.pid}")
    print("[WebUI] Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
            if process.poll() is not None:
                print(f"[WebUI] Server process exited unexpectedly with code {process.returncode}")
                break
    except KeyboardInterrupt:
        print("\n[WebUI] Ctrl+C detected. Terminating server process...")
        process.terminate()
        try:
            process.wait(timeout=3)
            print("[WebUI] Server terminated gracefully.")
        except subprocess.TimeoutExpired:
            print("[WebUI] Server did not exit. Killing...")
            process.kill()
            print("[WebUI] Server killed.")
    finally:
        sys.stdout.flush()
        os._exit(0)
