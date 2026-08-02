import asyncio
import datetime
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
    File,
    FastAPI,
    Form,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    status,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from utils.logger import logger

# Add src directory to path to import Coordinator
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from fastapi.security import OAuth2PasswordRequestForm

from agents.coordinator import Coordinator
from core.agent_runtime import AgentRuntime, ToolRegistry
from core.evidence import EvidenceService, S3EvidenceStore
from core.jobs import JobService, KubernetesJobBackend
from core.runtime_tools import register_coordinator_tools
from memory.context import ContextService
from memory.governance import MemoryGovernance
from storage.postgres import PostgresDatabase
from storage.audit import AuditLog
from config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_MODE,
    DATABASE_URL,
    DATA_DIR,
    INDEX_VERSION,
    MODEL_VERSION,
    S3_ACCESS_KEY as MINIO_ACCESS_KEY,
    S3_BUCKET as MINIO_BUCKET,
    S3_ENDPOINT as MINIO_ENDPOINT,
    S3_SECRET_KEY as MINIO_SECRET_KEY,
    validate_config,
)
from harness.product_loop import (
    DocumentRejected,
    build_input_descriptor,
    sha256_bytes,
    validate_upload,
)
from utils.auth import (
    create_access_token,
    decode_identity,
    get_current_identity,
    verify_password,
)
from utils.oidc import begin as begin_oidc
from utils.oidc import finish as finish_oidc
from utils.user_db import get_user
from utils.user_db import init_user_db
from utils.s3_utils import S3Utils

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
    logger.info("Starting background knowledge sync...")
    coordinator.start_knowledge_sync()
    yield
    # Shutdown
    logger.info("Shutting down and releasing resources...")
    try:
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
register_coordinator_tools(tool_registry, coordinator)
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
audit_log = AuditLog(DATABASE_URL)
_context_service_instance: ContextService | None = None


def _cache_scope(identity: dict) -> str:
    return ":".join((identity["tenant_id"], identity["username"], MODEL_VERSION, INDEX_VERSION))


def _require_admin(identity: dict):
    if identity["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required"
        )


def _context_service() -> ContextService:
    global _context_service_instance
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    agent_c = coordinator.agent_manager.agent_c
    if _context_service_instance is None:
        _context_service_instance = ContextService(
            DATABASE_URL, retriever=agent_c.retriever, memory=agent_c.memory
        )
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
        {"name": "feedback", "state": "waiting_for_input", "reason": "submit feedback for this run"},
        {"name": "memory", "state": "waiting_for_input", "reason": "H4 memory distillation and policy run is required"},
        {"name": "training_candidate", "state": "not_eligible", "reason": "feedback review and H5 snapshot gate are required"},
        {"name": "lora", "state": "blocked_by_phase", "reason": "H5 training and fixed evaluation are required"},
        {"name": "evaluation", "state": "blocked_by_phase", "reason": "H5 evaluation gate is required"},
        {"name": "release", "state": "blocked_by_phase", "reason": "H5 release governance is required"},
    ]
    artifacts = [artifact for item in tools for artifact in item["result"].get("artifacts", [])]
    approvals = [
        {"event_type": event["event_type"], "occurred_at": event["occurred_at"], "payload": event["payload"]}
        for event in agent_runtime.events(task["task_id"], identity)
        if "approval" in event["event_type"]
    ]
    timeline = [
        {"kind": "event", "at": event["occurred_at"], "type": event["event_type"], "payload": event["payload"]}
        for event in agent_runtime.events(task["task_id"], identity)
    ]
    timeline.extend(
        {"kind": "tool", "at": item["started_at"], "type": item["tool_name"], "step_id": item["step_id"], "state": item["state"]}
        for item in tools
    )
    return {
        "stages": stages,
        "timeline": sorted(timeline, key=lambda item: (str(item.get("at")), str(item.get("type")))),
        "artifacts": artifacts,
        "approvals": approvals,
        "verifications": verifications,
        "gates": future_gates,
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
            context_service.build_context(session_id, query, identity)

            await websocket.send_json({"type": "status", "content": "Retrieving knowledge..."})

            # 1. Agent C: Retrieve Knowledge
            self_coord = coordinator
            self_coord.agent_manager.lazy_load_agents(need_c=True)
            loop = asyncio.get_event_loop()
            context = await loop.run_in_executor(
                None, self_coord.agent_manager.agent_c.query, query, identity
            )

            await websocket.send_json({"type": "status", "content": "Consulting LoRA model..."})

            # 2. Agent B: Get Model Intuition
            self_coord.agent_manager.lazy_load_agents(need_b=True)
            intuition = await self_coord.agent_manager.agent_b.predict_async(
                query, cache_scope=_cache_scope(identity)
            )

            await websocket.send_json({"type": "status", "content": "Fusing response..."})

            # 3. Agent D: Final Fusion
            self_coord.agent_manager.lazy_load_agents(need_d=True)
            final_answer = await loop.run_in_executor(
                None, self_coord.agent_manager.agent_d.fuse_and_respond, query, context, intuition
            )

            # Save feedback
            feedback_id = self_coord.save_feedback(
                query,
                final_answer,
                owner=username,
                tenant_id=tenant_id,
                run_id=request_data.get("run_id"),
            )

            citations = [
                {
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "source_uri": item.get("source"),
                    "source_version": item.get("metadata", {}).get("source_version")
                    or item.get("document_version"),
                    "locator": item.get("metadata", {}).get("locator"),
                }
                for item in context
                if item.get("context_type") == "document" and item.get("chunk_id")
            ]

            context_service.append_event(
                session_id,
                "assistant_message",
                {"content": final_answer, "citations": citations, "user_event_id": user_event["event_id"]},
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
                }
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.send_json({"error": str(e)})
        except:
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
    citations: list[dict[str, Any]] = Field(default_factory=list)


class FeedbackUpdateRequest(BaseModel):
    feedback_id: str
    feedback: str  # "good" or "bad"


class FeedbackReviewRequest(BaseModel):
    feedback_id: str
    review_status: str


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


@app.post("/api/models/reload", response_model=ReloadResponse)
async def reload_model(identity: dict = Depends(get_current_identity)):
    """Force the WebUI to reload the latest LoRA adapter from S3."""
    _require_admin(identity)
    try:
        # Run in executor as it might involve S3 downloads and model loading
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, coordinator.reload_model)

        if success:
            return {"status": "success", "message": "Latest model adapter loaded from S3."}
        else:
            return {"status": "skipped", "message": "Model is already up to date or reload failed."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reloading model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    return {"session_id": session["session_id"], "version": session["version"], "authority": "postgresql"}


@app.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        session = _context_service().get_session(session_id, identity)
        messages = _context_service().events(session_id, identity)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session": session, "messages": messages, "authority": "postgresql"}


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, request: SessionPatch, identity: dict = Depends(get_current_identity)):
    if request.auto_memory_enabled is None:
        return _context_service().get_session(session_id, identity)
    try:
        return _context_service().set_auto_memory(
            session_id, request.auto_memory_enabled, identity, request.expected_version
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/sessions/{session_id}/context")
async def get_session_context(session_id: str, query: str = "", identity: dict = Depends(get_current_identity)):
    try:
        envelope = _context_service().build_context(session_id, query or "", identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return {
        key: envelope[key]
        for key in (
            "snapshot_id", "task", "packs", "handoff", "recent_event_ids", "document_chunk_ids",
            "memory_ids", "budget", "envelope_sha256",
        )
    }


@app.post("/api/sessions/{session_id}/close")
async def close_session(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        service = _context_service()
        checkpoint = service.compact(session_id, identity)
        service.append_event(session_id, "session_closed", {"checkpoint_id": checkpoint["checkpoint_id"]}, identity)
        with service.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversation_sessions SET state = 'closed', closed_at = now(), version = version + 1, updated_at = now() "
                    "WHERE session_id = %s AND owner_id = %s AND state = 'active'",
                    (session_id, identity["username"]),
                )
        distillation = _distill_session(session_id, identity, service)
        return {"session_id": session_id, "state": "closed", "checkpoint": checkpoint, "distillation": distillation}
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


def _distill_session(session_id: str, identity: dict[str, str], service: ContextService | None = None) -> dict[str, Any]:
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
async def reset_session(session_id: str, expected_version: int, identity: dict = Depends(get_current_identity)):
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
            except PermissionError:
                raise HTTPException(status_code=404, detail="Session not found")
            if session["state"] != "active":
                raise HTTPException(status_code=409, detail="Session is not active")
        else:
            session = context_service.create_session(identity)
            session_id = session["session_id"]

        user_event = context_service.append_event(
            session_id, "user_message", {"content": request.query}, identity
        )
        # Snapshot creation is durable evidence for the model call. The existing
        # Coordinator remains the execution path; its retriever still enforces RLS.
        context_service.build_context(session_id, request.query, identity)

        # Keep the answer path identical while retaining the retriever rows that
        # generated citations for the H3 run detail view.
        answer, citations = await coordinator.chat_with_citations_async(
            request.query, identity, cache_scope=_cache_scope(identity)
        )

        context_service.append_event(
            session_id,
            "assistant_message",
            {"content": answer, "citations": citations, "user_event_id": user_event["event_id"]},
            identity,
            trust_label="trusted_system",
        )

        # Save feedback record (file-based)
        feedback_id = coordinator.save_feedback(
            request.query,
            answer,
            owner=identity["username"],
            tenant_id=identity["tenant_id"],
            run_id=request.run_id,
        )
        return ChatResponse(
            answer=answer,
            feedback_id=feedback_id,
            session_id=session_id,
            citations=citations,
        )
    except Exception as e:
        logger.error(f"Error during chat: {e}", exc_info=True)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))


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
        readers = json.loads(acl) if acl.strip() else [
            {"subject_type": "user", "subject_id": identity["username"], "permission": "read"}
        ]
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
            {"criterion_id": "input", "verifier": "verify_input_manifest", "version": 1, "parameters": {}, "phase": "after_step", "required": True},
            {"criterion_id": "rough", "verifier": "verify_rough_clean", "version": 2, "parameters": {}, "phase": "after_step", "required": True},
            {"criterion_id": "refine", "verifier": "verify_refined_corpus", "version": 1, "parameters": {}, "phase": "after_step", "required": True},
            {"criterion_id": "publish", "verifier": "verify_ingest", "version": 2, "parameters": {"expected_phrase": expected_phrase}, "phase": "after_step", "required": True},
            {"criterion_id": "retrieval", "verifier": "verify_retrieval", "version": 2, "parameters": {"query": question}, "phase": "after_step", "required": True},
        ]
        plan = [
            {"tool": "validate_document_input", "arguments": {"input_key": descriptor_key, "input_sha256": sha256_bytes(body)}, "scope_refs": [descriptor_ref], "verifier_refs": ["input"]},
            {"tool": "spark_rough_clean", "arguments": {"input_key": f"s3a://{MINIO_BUCKET}/{raw_prefix}", "input_sha256": sha256_bytes(body)}, "scope_refs": [raw_ref], "verifier_refs": ["rough"]},
            {"tool": "refine_corpus", "arguments": {"input_key": descriptor_key}, "scope_refs": [descriptor_ref], "verifier_refs": ["refine"]},
            {"tool": "publish_corpus", "arguments": {"input_key": descriptor_key}, "scope_refs": [descriptor_ref, postgres_ref], "verifier_refs": ["publish"]},
            {"tool": "rag_probe", "arguments": {"query": question}, "scope_refs": [postgres_ref], "verifier_refs": ["retrieval"]},
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
        return {"run_id": task["run_id"], "task_id": task["task_id"], "input": descriptor, "task": task}
    except (DocumentRejected, json.JSONDecodeError, KeyError, PermissionError, RuntimeError, ValueError) as error:
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
            return await agent_runtime.reconcile_job(task["task_id"], identity, request.expected_version)
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

        # 3. Upload back
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, ensure_ascii=False, indent=2),
            ContentType="application/json",
        )

        logger.info(f"Feedback updated in S3 for {request.feedback_id} to {request.feedback}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error updating feedback in S3: {e}")
        raise HTTPException(status_code=500, detail=f"S3 Update failed: {str(e)}")


@app.post("/api/feedback/review")
async def review_feedback(
    request: FeedbackReviewRequest, identity: dict = Depends(get_current_identity)
):
    """Approve or reject a feedback record before it can become training data."""
    _require_admin(identity)
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
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, ensure_ascii=False, indent=2),
            ContentType="application/json",
        )
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as error:
        logger.error(f"Error reviewing feedback: {error}")
        raise HTTPException(status_code=500, detail="Feedback review failed") from error


def _memory_orchestrator():
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    return coordinator.agent_manager.agent_c.memory


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
        "memories": orchestrator.retrieve(query, identity) if query.strip() else orchestrator.list(identity),
        "authority": "postgresql",
    }


@app.post("/api/memories/preview")
async def preview_memory(request: MemoryCreateRequest, identity: dict = Depends(get_current_identity)):
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
        MemoryGovernance(DATABASE_URL).resolve_conflict(
            memory_id, identity, request.policy_version
        )
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
