"""Chat, session, and task routes."""

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

from core.evidence import sha256
from feedback import save_feedback
from memory.context import ContextService
from rag.answering import answer_with_citations
from utils.auth import decode_identity, get_current_identity
from utils.logger import logger
from webui import state as runtime
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
async def websocket_endpoint(  # noqa: C901 - protocol loop handles independent message cases
    websocket: WebSocket,
):
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
            context_service = runtime._context_service()

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
                runtime._adapter_runtime,
                runtime._answering,
                cache_scope=runtime._cache_scope(identity),
            )

            # Save feedback
            feedback_id = save_feedback(
                runtime._evidence_s3,
                query,
                final_answer,
                owner=username,
                tenant_id=tenant_id,
                run_id=request_data.get("run_id"),
                citations=citations,
                retrieval_report={
                    "context_snapshot_id": envelope.get("snapshot_id"),
                    "context_sha256": envelope.get("envelope_sha256"),
                },
                model_execution=model_execution,
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
        envelope = runtime._context_service().build_context(session_id, query or "", identity)
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
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    try:
        context_service = runtime._context_service()
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
        context_ref, context_object_sha256 = runtime._publish_chat_context(
            envelope, identity, run_id
        )
        document_ids = sorted(
            {
                str(item["document_id"])
                for item in envelope["retrieval_context"]
                if item.get("document_id")
            }
        )
        task = runtime.agent_runtime.create_task(
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
        completed = await runtime.agent_runtime.run(task["task_id"], identity)
        tool_runs = runtime.agent_runtime.tool_runs(task["task_id"], identity)
        output = tool_runs[-1]["result"]["output"] if tool_runs else {}
        response_ref = output.get("response_ref")
        response_sha256 = output.get("response_sha256")
        response_body = (
            runtime._evidence_store.get(response_ref) if isinstance(response_ref, str) else None
        )
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
