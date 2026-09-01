"""Feedback, connector, and memory routes."""

import hashlib
import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from config import DATABASE_URL
from feedback import rate_feedback
from harness.evaluation import EvaluationService
from memory.governance import MemoryGovernance
from storage.postgres import PostgresDatabase
from utils.auth import get_current_identity
from webui import state as runtime
from webui.schemas import (
    FeedbackUpdateRequest,
    MemoryApprovalRequest,
    MemoryConflictResolveRequest,
    MemoryCreateRequest,
    MemoryDecisionRequest,
    MemoryRevisionRequest,
)

router = APIRouter()


@router.post("/api/feedback")
async def update_feedback(
    request: FeedbackUpdateRequest, identity: dict = Depends(get_current_identity)
):
    """Create an immutable rating and its PostgreSQL annotation."""
    try:
        annotation_id = rate_feedback(
            runtime._evidence_s3,
            EvaluationService(DATABASE_URL),
            identity,
            request.feedback_id,
            request.feedback,
        )
        return {"status": "success", "annotation_id": annotation_id}
    except (FileNotFoundError, PermissionError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (json.JSONDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _memory_orchestrator():
    return runtime._memory


@router.get("/api/connectors/runs")
async def list_connector_runs(identity: dict = Depends(get_current_identity)):
    with PostgresDatabase(DATABASE_URL).transaction(identity) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id, connector_id, state, cursor_before, cursor_after, "
                "error_summary, started_at, completed_at FROM connector_runs "
                "ORDER BY started_at DESC LIMIT 50"
            )
            return {"runs": [{**row, "run_id": str(row["run_id"])} for row in cursor.fetchall()]}


@router.get("/api/memories")
async def list_memories(query: str, identity: dict = Depends(get_current_identity)):
    orchestrator = _memory_orchestrator()
    return {
        "memories": orchestrator.retrieve(query, identity)
        if query.strip()
        else orchestrator.list(identity),
        "authority": "postgresql",
    }


@router.post("/api/memories/preview")
async def preview_memory(
    request: MemoryCreateRequest, identity: dict = Depends(get_current_identity)
):
    service = runtime._context_service()
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


@router.post("/api/memories")
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


@router.post("/api/memories/{memory_id}/approval")
async def approve_memory(
    memory_id: str, request: MemoryApprovalRequest, identity: dict = Depends(get_current_identity)
):
    try:
        if not request.approved:
            _memory_orchestrator().reject(memory_id, identity)
            return {"memory_id": memory_id, "status": "rejected"}
        runtime._require_admin(identity)
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        _memory_orchestrator().approve(memory_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"memory_id": memory_id, "status": "approved"}


@router.post("/api/memories/{memory_id}/decision")
async def decide_memory(
    memory_id: str, request: MemoryDecisionRequest, identity: dict = Depends(get_current_identity)
):
    try:
        if request.decision == "approve":
            runtime._require_admin(identity)
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


@router.post("/api/memories/{memory_id}/resolve-conflict")
async def resolve_memory_conflict(
    memory_id: str,
    request: MemoryConflictResolveRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_admin(identity)
    try:
        MemoryGovernance(DATABASE_URL).resolve_conflict(memory_id, identity, request.policy_version)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"memory_id": memory_id, "status": "approved", "policy_version": request.policy_version}


@router.put("/api/memories/{memory_id}")
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


@router.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: str, identity: dict = Depends(get_current_identity)):
    try:
        request_id = _memory_orchestrator().delete("memory", memory_id, identity)
    except PermissionError as error:
        raise HTTPException(status_code=404, detail="Memory not found") from error
    return {"request_id": request_id, "status": "completed"}
