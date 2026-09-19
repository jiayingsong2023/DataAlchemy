"""Chat, session, and task routes."""

import asyncio
import json
import uuid
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import ValidationError

from core.agent_runtime import AgentRuntime
from core.evidence import sha256
from core.verifiers import ReadOnlyServices
from feedback import save_feedback
from memory.context import ContextService
from utils.auth import decode_identity, get_current_identity
from utils.logger import logger
from webui import state as runtime
from webui.chat_requests import ChatRequests, RequestConflict, RequestInProgress
from webui.schemas import (
    ChatRequest,
    ChatResponse,
    SessionCreate,
    SessionPatch,
    TaskApprovalRequest,
    TaskControlRequest,
    TaskCreateRequest,
    TaskReplanRequest,
)

router = APIRouter()


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    identity = decode_identity(token) if token else None
    if not identity:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            try:
                request = ChatRequest.model_validate_json(data)
            except ValidationError:
                await websocket.send_json({"error": "Invalid chat request", "status_code": 422})
                continue
            try:
                response = await _execute_chat(request, identity)
            except HTTPException as error:
                await websocket.send_json({"error": error.detail, "status_code": error.status_code})
                continue
            await websocket.send_json(
                {"type": "answer", "content": response.answer, **response.model_dump()}
            )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")


@router.get("/api/sessions")
async def list_sessions(identity: dict = Depends(get_current_identity)):
    sessions = runtime._context_service().list_sessions(identity)
    logger.info("API: Found %s durable sessions for user %s", len(sessions), identity["username"])
    return {"sessions": sessions, "authority": "postgresql"}


@router.post("/api/sessions")
async def create_session(request: SessionCreate, identity: dict = Depends(get_current_identity)):
    session = runtime._context_service().create_session(
        identity, request.title or "New Chat", request.auto_memory_enabled
    )
    return {
        "session_id": session["session_id"],
        "version": session["version"],
        "authority": "postgresql",
    }


@router.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        session = runtime._context_service().get_session(session_id, identity)
        messages = runtime._context_service().events(session_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return {"session": session, "messages": messages, "authority": "postgresql"}


@router.patch("/api/sessions/{session_id}")
async def patch_session(
    session_id: str, request: SessionPatch, identity: dict = Depends(get_current_identity)
):
    if request.auto_memory_enabled is None:
        return runtime._context_service().get_session(session_id, identity)
    try:
        return runtime._context_service().set_auto_memory(
            session_id, request.auto_memory_enabled, identity, request.expected_version
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/sessions/{session_id}/context")
async def get_session_context(
    session_id: str, query: str = "", identity: dict = Depends(get_current_identity)
):
    try:
        envelope = await asyncio.to_thread(
            runtime._context_service().build_context, session_id, query or "", identity
        )
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


@router.post("/api/sessions/{session_id}/close")
async def close_session(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        service = runtime._context_service()
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
    service = service or runtime._context_service()
    session = service.get_session(session_id, identity)
    candidates = service.extract_candidates(service.events(session_id, identity))
    orchestrator = runtime._memory
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


@router.post("/api/sessions/{session_id}/distill")
async def distill_session(session_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return _distill_session(session_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


@router.post("/api/sessions/{session_id}/reset")
async def reset_session(
    session_id: str, expected_version: int, identity: dict = Depends(get_current_identity)
):
    try:
        return runtime._context_service().reset(session_id, identity, expected_version)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


@router.post("/api/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    task_spec_sha256: Optional[str] = None,
    plan_version: Optional[int] = None,
    identity: dict = Depends(get_current_identity),
):
    try:
        return runtime._context_service().resume(
            session_id,
            identity,
            task_spec_sha256=task_spec_sha256,
            plan_version=plan_version,
        )
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/history")
async def get_history(identity: dict = Depends(get_current_identity)):
    # Legacy shape, backed by the durable session store during migration.
    service = runtime._context_service()
    history = []
    for session in service.list_sessions(identity):
        history.extend(service.events(session["session_id"], identity))
    return {"history": history, "deprecated": True, "authority": "postgresql"}


@router.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, identity: dict = Depends(get_current_identity)):
    return await _execute_chat(request, identity)


async def _execute_chat(request: ChatRequest, identity: dict[str, str]) -> ChatResponse:
    """Both transports share task execution and, when supplied, a durable request key."""
    try:
        if request.request_id is None:
            return await _run_chat(request, identity)
        requests = ChatRequests(runtime.agent_runtime.database)
        try:
            requests.key(str(request.request_id), request.session_id, identity)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid session ID") from error
        with requests.claim(
            str(request.request_id), request.session_id, request.query, identity
        ) as receipt:
            return await _run_chat(request, identity, receipt, requests)
    except (RequestConflict, RequestInProgress) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except Exception as error:
        logger.exception("Chat execution failed")
        raise HTTPException(status_code=500, detail="chat_execution_failed") from error


def _chat_session(request: ChatRequest, identity: dict[str, str], receipt: dict | None) -> dict:
    service = runtime._context_service()
    if not request.session_id:
        options = {"session_id": receipt["session_id"]} if receipt else {}
        return service.create_session(identity, **options)
    try:
        return service.get_session(request.session_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


async def _chat_context(
    request: ChatRequest,
    identity: dict[str, str],
    session_id: str,
    task_id: str,
    run_id: str,
    receipt: dict | None,
    requests: ChatRequests | None,
) -> tuple[dict, str, str]:
    if receipt and receipt["context_ref"]:
        ref, digest = receipt["context_ref"], receipt["context_sha256"]
        envelope = runtime._load_chat_context(ref, digest)
        document_ids = {
            str(item["document_id"])
            for item in envelope["retrieval_context"]
            if item.get("document_id")
        }
        services = ReadOnlyServices(runtime.agent_runtime.verifier_database_url, identity)
        documents = services.documents(sorted(document_ids))
        if {str(item["document_id"]) for item in documents} != document_ids or any(
            item["status"] != "ready" for item in documents
        ):
            raise HTTPException(status_code=409, detail="chat_context_no_longer_authorized")
        return envelope, ref, digest
    envelope = await asyncio.to_thread(
        runtime._context_service().build_context,
        session_id,
        request.query,
        identity,
        task_type="rag_chat",
        task={"task_id": task_id, "run_id": run_id, "plan_version": 1},
    )
    ref, digest = runtime._publish_chat_context(envelope, identity, run_id)
    if receipt and requests:
        requests.save_context(receipt, ref, digest, identity)
    return envelope, ref, digest


def _create_chat_task(
    identity: dict[str, str],
    envelope: dict,
    context_ref: str,
    context_object_sha256: str,
    task_id: str,
    run_id: str,
) -> dict:
    document_ids = sorted(
        {
            str(item["document_id"])
            for item in envelope["retrieval_context"]
            if item.get("document_id")
        }
    )
    return runtime.agent_runtime.create_task(
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
                    "version": 2,
                    "parameters": {
                        "snapshot_id": envelope["snapshot_id"],
                        "context_sha256": envelope["envelope_sha256"],
                        "document_ids": document_ids,
                        "context_ref": context_ref,
                        "context_object_sha256": context_object_sha256,
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


def _chat_response(task: dict, identity: dict[str, str], *, replay: bool) -> dict:
    if task["state"] in AgentRuntime.terminal_states - {"succeeded"}:
        raise HTTPException(status_code=409, detail=f"chat_task_{task['state']}")
    tools = runtime.agent_runtime.tool_runs(task["task_id"], identity)
    result = tools[-1]["result"] if tools else {}
    output = result.get("output", {})
    ref = output.get("response_ref")
    body = runtime._evidence_store.get(ref) if isinstance(ref, str) else None
    if (
        task["state"] != "succeeded"
        or body is None
        or sha256(body) != output.get("response_sha256")
    ):
        raise RuntimeError("rag_chat_response_not_verified")
    if replay:
        # Reuse the read-only verifier against CURRENT ACLs rather than caching permission.
        criterion = task["task_spec"]["success_criteria"][0]
        services = ReadOnlyServices(runtime.agent_runtime.verifier_database_url, identity)
        verifier = runtime.agent_runtime.verifiers.get(criterion["verifier"], criterion["version"])
        checked = verifier.handler(criterion, task, result, services)
        documents = services.documents(criterion["parameters"]["document_ids"])
        if checked.status != "passed" or any(item["status"] != "ready" for item in documents):
            raise HTTPException(status_code=409, detail="chat_response_no_longer_authorized")
    return json.loads(body)


async def _run_chat(
    request: ChatRequest,
    identity: dict[str, str],
    receipt: dict | None = None,
    requests: ChatRequests | None = None,
) -> ChatResponse:
    service = runtime._context_service()
    task = None
    if receipt:
        try:
            task = runtime.agent_runtime.get_task(receipt["task_id"], identity)
        except PermissionError:
            pass  # Receipt committed before the task was created.
    session = _chat_session(request, identity, receipt)
    session_id = session["session_id"]
    if session.get("state") == "deleted":
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("state", "active") != "active" and not (task and task["state"] == "succeeded"):
        raise HTTPException(status_code=409, detail="Session is not active")
    task_id = receipt["task_id"] if receipt else str(uuid.uuid4())
    run_id = receipt["run_id"] if receipt else str(uuid.uuid4())
    user_options = {"event_id": str(uuid.uuid5(uuid.UUID(run_id), "user"))} if receipt else {}
    user_event = service.append_event(
        session_id,
        "user_message",
        {"content": request.query},
        identity,
        task_id=task_id,
        run_id=run_id,
        **user_options,
    )
    if receipt and user_event["generation"] != session["context_generation"]:
        raise HTTPException(status_code=409, detail="chat_context_generation_changed")
    envelope, ref, digest = await _chat_context(
        request, identity, session_id, task_id, run_id, receipt, requests
    )
    replay = task is not None
    if task is None:
        task = _create_chat_task(identity, envelope, ref, digest, task_id, run_id)
    completed = (
        task
        if task.get("state") == "succeeded"
        else await runtime.agent_runtime.run(task_id, identity)
    )
    response = _chat_response(completed, identity, replay=replay)
    answer, citations, model_execution = (
        response["answer"],
        response["citations"],
        response["model_execution"],
    )
    contract = {
        name: response.get(name)
        for name in ("answer_status", "answer_mode", "support_status", "reason_code")
    }
    assistant_options = (
        {"event_id": str(uuid.uuid5(uuid.UUID(run_id), "assistant"))} if receipt else {}
    )
    service.append_event(
        session_id,
        "assistant_message",
        {
            "content": answer,
            "citations": citations,
            "user_event_id": user_event["event_id"],
            **(
                {"answer_contract": contract}
                if response.get("schema_version") == "rag_chat_response.v2"
                else {}
            ),
        },
        identity,
        trust_label="trusted_system",
        task_id=task_id,
        run_id=run_id,
        **assistant_options,
    )
    feedback_options = (
        {
            "feedback_id": f"feedback_{run_id}.json",
            "timestamp": receipt["created_at"].isoformat(),
            "evidence_store": runtime._evidence_store,
        }
        if receipt
        else {}
    )
    feedback_id = save_feedback(
        runtime._evidence_s3,
        request.query,
        answer,
        owner=identity["username"],
        tenant_id=identity["tenant_id"],
        run_id=run_id,
        citations=citations,
        retrieval_report={
            "context_snapshot_id": envelope.get("snapshot_id"),
            "context_sha256": envelope.get("envelope_sha256"),
        },
        model_execution=model_execution,
        **(
            {"answer_contract": contract, "answer_policy_version": "rag-answer-v2"}
            if response.get("schema_version") == "rag_chat_response.v2"
            else {}
        ),
        **feedback_options,
    )
    return ChatResponse(
        answer=answer,
        feedback_id=feedback_id,
        session_id=session_id,
        run_id=run_id,
        task_id=task_id,
        request_id=str(request.request_id) if request.request_id else None,
        citations=citations,
        model_execution=model_execution,
        **contract,
        execution_status=response.get("execution_status"),
    )


@router.get("/api/chat/requests/{request_id}")
async def chat_request_status(
    request_id: uuid.UUID,
    session_id: str | None = None,
    identity: dict = Depends(get_current_identity),
):
    requests = ChatRequests(runtime.agent_runtime.database)
    try:
        key = requests.key(str(request_id), session_id, identity)
        receipt = requests.get(key, identity)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid session ID") from error
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Chat request not found") from error
    try:
        task = runtime.agent_runtime.get_task(receipt["task_id"], identity)
        task_state = task["state"]
    except PermissionError:
        task_state = "initializing"
    return {name: receipt[name] for name in ("request_id", "session_id", "task_id", "run_id")} | {
        "state": task_state
    }


def _task_http_error(error: Exception) -> HTTPException:
    if isinstance(error, (KeyError, PermissionError)):
        return HTTPException(status_code=404, detail="Task not found")
    return HTTPException(status_code=400, detail=str(error))


@router.post("/api/tasks")
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
        task = runtime.agent_runtime.create_task(
            identity,
            request.goal,
            plan,
            max_steps,
            execution_mode=request.execution_mode,
            task_spec=task_spec,
        )
        return await runtime.agent_runtime.run(task["task_id"], identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.get("/api/tasks")
async def list_tasks(identity: dict = Depends(get_current_identity)):
    return {"tasks": runtime.agent_runtime.list_tasks(identity)}


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return runtime.agent_runtime.get_task(task_id, identity)
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@router.get("/api/tasks/{task_id}/events")
async def get_task_events(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return {"events": runtime.agent_runtime.events(task_id, identity)}
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@router.get("/api/tasks/{task_id}/verifications")
async def get_task_verifications(task_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return {"verifications": runtime.agent_runtime.verifications(task_id, identity)}
    except (KeyError, PermissionError) as error:
        raise _task_http_error(error) from error


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, identity: dict = Depends(get_current_identity)):
    try:
        task = next(
            task for task in runtime.agent_runtime.list_tasks(identity) if task["run_id"] == run_id
        )
        return {
            "task": task,
            "evidence": runtime.agent_runtime.evidence_status(task["task_id"], identity),
            **runtime._run_details(task, identity),
        }
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@router.get("/api/runs/{run_id}/manifest")
async def get_run_manifest(run_id: str, identity: dict = Depends(get_current_identity)):
    if runtime.agent_runtime.evidence is None:
        raise HTTPException(status_code=503, detail="Evidence publishing is not configured")
    try:
        return runtime.agent_runtime.evidence.manifest(run_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Published manifest not found") from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/runs/{run_id}/reconcile")
async def reconcile_run(
    run_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    runtime._require_admin(identity)
    try:
        task = next(
            task for task in runtime.agent_runtime.list_tasks(identity) if task["run_id"] == run_id
        )
        if task["state"] in {"waiting_job", "cancelling"}:
            return await runtime.agent_runtime.reconcile_job(
                task["task_id"], identity, request.expected_version
            )
        return runtime.agent_runtime.reconcile_evidence(
            task["task_id"], identity, request.expected_version
        )
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.delete("/api/runs/{run_id}/manifest")
async def delete_run_manifest(run_id: str, identity: dict = Depends(get_current_identity)):
    runtime._require_admin(identity)
    try:
        task = next(
            task for task in runtime.agent_runtime.list_tasks(identity) if task["run_id"] == run_id
        )
        runtime.agent_runtime.delete_evidence(task["task_id"], identity)
        return {"status": "deleted", "run_id": run_id}
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return runtime.agent_runtime.pause(task_id, identity, request.expected_version)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        runtime.agent_runtime.resume(task_id, identity, request.expected_version)
        return await runtime.agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        runtime.agent_runtime.retry(task_id, identity, request.expected_version)
        return await runtime.agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/retry-verification")
async def retry_task_verification(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        runtime.agent_runtime.retry_verification(task_id, identity, request.expected_version)
        return await runtime.agent_runtime.run(task_id, identity)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/approval")
async def approve_task(
    task_id: str,
    request: TaskApprovalRequest,
    identity: dict = Depends(get_current_identity),
):
    try:
        task = runtime.agent_runtime.approve(
            task_id, identity, request.approved, request.expected_version
        )
        return await runtime.agent_runtime.run(task_id, identity) if request.approved else task
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: TaskControlRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return runtime.agent_runtime.cancel(task_id, identity, request.expected_version)
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error


@router.post("/api/tasks/{task_id}/replan")
async def replan_task(
    task_id: str, request: TaskReplanRequest, identity: dict = Depends(get_current_identity)
):
    try:
        return runtime.agent_runtime.replan(
            task_id, identity, request.remaining_steps, request.reason, request.expected_version
        )
    except (KeyError, PermissionError, RuntimeError, ValueError) as error:
        raise _task_http_error(error) from error
