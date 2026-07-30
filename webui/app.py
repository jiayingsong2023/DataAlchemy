import asyncio
import datetime
import json
import logging
import os
import sys
import time
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
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
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
from core.runtime_tools import register_coordinator_tools
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
agent_runtime = AgentRuntime(DATABASE_URL, tool_registry)
audit_log = AuditLog(DATABASE_URL)


def _cache_scope(identity: dict) -> str:
    return ":".join((identity["tenant_id"], identity["username"], MODEL_VERSION, INDEX_VERSION))


def _require_admin(identity: dict):
    if identity["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required"
        )


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

            if session_id:
                self_coord = coordinator
                self_coord.agent_manager.lazy_load_agents(need_b=True)
                self_coord.agent_manager.agent_b._ensure_engine()
                try:
                    await self_coord.agent_manager.agent_b.batch_engine.cache.require_session_owner(
                        username, session_id, tenant_id
                    )
                except PermissionError:
                    await websocket.send_json({"error": "Session not found"})
                    continue

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

            # Determine session
            self_coord.agent_manager.agent_b._ensure_engine()
            if not session_id:
                session_id = (
                    await self_coord.agent_manager.agent_b.batch_engine.cache.create_session(
                        username, tenant_id=tenant_id
                    )
                )

            # Save to Redis session history
            await self_coord.agent_manager.agent_b.batch_engine.cache.add_message_to_session(
                username,
                session_id,
                {
                    "query": query,
                    "answer": final_answer,
                    "timestamp": datetime.datetime.now().isoformat(),
                },
                tenant_id=tenant_id,
            )

            # Save feedback
            feedback_id = self_coord.save_feedback(
                query, final_answer, owner=username, tenant_id=tenant_id
            )

            # Send final answer
            await websocket.send_json(
                {
                    "type": "answer",
                    "content": final_answer,
                    "feedback_id": feedback_id,
                    "session_id": session_id,
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


class SessionCreate(BaseModel):
    title: Optional[str] = "New Chat"


class ChatResponse(BaseModel):
    answer: str
    feedback_id: str
    session_id: str


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
    tool: str = "rag_chat"
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None
    max_steps: int = Field(default=8, ge=1, le=8)


class TaskApprovalRequest(BaseModel):
    approved: bool


@app.post("/api/jobs/full-cycle")
async def trigger_full_cycle(identity: dict = Depends(get_current_identity)):
    """Trigger a full cycle Job via Kubernetes Annotation."""
    _require_admin(identity)
    try:
        from kubernetes import client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        custom_api = client.CustomObjectsApi()

        # Get current time as timestamp
        timestamp = str(int(time.time()))

        # Patch the DataAlchemyStack resource
        namespace = "data-alchemy"  # Should ideally be configurable
        name = "data-alchemy"  # Should ideally be configurable

        body = {"metadata": {"annotations": {"dataalchemy.io/request-full-cycle": timestamp}}}

        custom_api.patch_namespaced_custom_object(
            group="dataalchemy.io",
            version="v1alpha1",
            namespace=namespace,
            plural="dataalchemystacks",
            name=name,
            body=body,
        )

        logger.info(f"Full cycle triggered by {identity['username']} at {timestamp}")
        return {"status": "success", "job_id": timestamp}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger full cycle: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
    coordinator.agent_manager.lazy_load_agents(need_b=True)
    coordinator.agent_manager.agent_b._ensure_engine()
    cache = coordinator.agent_manager.agent_b.batch_engine.cache
    sessions = await cache.list_sessions(identity["username"], identity["tenant_id"])
    logger.info(f"API: Found {len(sessions)} sessions for user {identity['username']}")
    return {"sessions": sessions}


@app.post("/api/sessions")
async def create_session(request: SessionCreate, identity: dict = Depends(get_current_identity)):
    coordinator.agent_manager.lazy_load_agents(need_b=True)
    coordinator.agent_manager.agent_b._ensure_engine()
    session_id = await coordinator.agent_manager.agent_b.batch_engine.cache.create_session(
        identity["username"], request.title, identity["tenant_id"]
    )
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, identity: dict = Depends(get_current_identity)):
    coordinator.agent_manager.lazy_load_agents(need_b=True)
    coordinator.agent_manager.agent_b._ensure_engine()
    try:
        messages = await coordinator.agent_manager.agent_b.batch_engine.cache.get_session_messages(
            identity["username"], session_id, identity["tenant_id"]
        )
    except PermissionError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"messages": messages}


@app.get("/api/history")
async def get_history(identity: dict = Depends(get_current_identity)):
    # Legacy endpoint
    coordinator.agent_manager.lazy_load_agents(need_b=True)
    coordinator.agent_manager.agent_b._ensure_engine()
    history = await coordinator.agent_manager.agent_b.batch_engine.cache.get_chat_history(
        identity["username"]
    )
    return {"history": history}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, identity: dict = Depends(get_current_identity)):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    try:
        coordinator.agent_manager.lazy_load_agents(need_b=True)
        coordinator.agent_manager.agent_b._ensure_engine()
        cache = coordinator.agent_manager.agent_b.batch_engine.cache
        if request.session_id:
            try:
                await cache.require_session_owner(
                    identity["username"], request.session_id, identity["tenant_id"]
                )
            except PermissionError:
                raise HTTPException(status_code=404, detail="Session not found")

        # Use Coordinator to get fused response (async)
        answer = await coordinator.chat_async(
            request.query, identity, cache_scope=_cache_scope(identity)
        )

        # Determine session
        session_id = request.session_id
        if not session_id:
            session_id = await cache.create_session(
                identity["username"], tenant_id=identity["tenant_id"]
            )

        # Save to Redis session history
        await cache.add_message_to_session(
            identity["username"],
            session_id,
            {
                "query": request.query,
                "answer": answer,
                "timestamp": datetime.datetime.now().isoformat(),
            },
            tenant_id=identity["tenant_id"],
        )

        # Save feedback record (file-based)
        feedback_id = coordinator.save_feedback(
            request.query, answer, owner=identity["username"], tenant_id=identity["tenant_id"]
        )
        return ChatResponse(answer=answer, feedback_id=feedback_id, session_id=session_id)
    except Exception as e:
        logger.error(f"Error during chat: {e}", exc_info=True)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=500, detail=str(e))


def _task_http_error(error: Exception) -> HTTPException:
    if isinstance(error, (KeyError, PermissionError)):
        return HTTPException(status_code=404, detail="Task not found")
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/tasks")
async def create_task(request: TaskCreateRequest, identity: dict = Depends(get_current_identity)):
    """Create and execute one durable single-agent task."""
    try:
        task = agent_runtime.create_task(
            identity,
            request.goal,
            [
                {
                    "tool": request.tool,
                    "arguments": request.arguments,
                    "idempotency_key": request.idempotency_key,
                }
            ],
            request.max_steps,
        )
        return await agent_runtime.run(task["task_id"], identity)
    except (KeyError, PermissionError, ValueError) as error:
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


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return agent_runtime.pause(task_id, identity)
    except (KeyError, PermissionError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        agent_runtime.resume(task_id, identity)
        return await agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        agent_runtime.retry(task_id, identity)
        return await agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, ValueError) as error:
        raise _task_http_error(error) from error


@app.post("/api/tasks/{task_id}/approval")
async def approve_task(
    task_id: str,
    request: TaskApprovalRequest,
    identity: dict = Depends(get_current_identity),
):
    try:
        task = agent_runtime.approve(task_id, identity, request.approved)
        return await agent_runtime.run(task_id, identity) if request.approved else task
    except (KeyError, PermissionError, ValueError) as error:
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
            return {
                "runs": [
                    {**row, "run_id": str(row["run_id"])}
                    for row in cursor.fetchall()
                ]
            }


@app.get("/api/memories")
async def list_memories(query: str, identity: dict = Depends(get_current_identity)):
    return {"memories": _memory_orchestrator().retrieve(query, identity)}


@app.post("/api/memories")
async def create_memory(
    request: MemoryCreateRequest, identity: dict = Depends(get_current_identity)
):
    try:
        memory_id = _memory_orchestrator().create_candidate(
            identity, request.kind, request.content, request.source_event_id
        )
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"memory_id": memory_id, "status": "candidate"}


@app.post("/api/memories/{memory_id}/approval")
async def approve_memory(
    memory_id: str, request: MemoryApprovalRequest, identity: dict = Depends(get_current_identity)
):
    if not request.approved:
        _memory_orchestrator().delete("memory", memory_id, identity)
        return {"memory_id": memory_id, "status": "deleted"}
    try:
        _memory_orchestrator().approve(memory_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Memory not found") from error
    return {"memory_id": memory_id, "status": "approved"}


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

import subprocess
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
