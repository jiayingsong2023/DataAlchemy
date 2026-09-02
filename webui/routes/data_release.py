"""Authentication, data, evaluation, and release routes."""

import asyncio
import json
import uuid
from datetime import timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm

from config import ACCESS_TOKEN_EXPIRE_MINUTES, AUTH_MODE, DATABASE_URL
from harness.evaluation import EvaluationService
from harness.pilot import PilotService
from harness.product_loop import (
    DocumentRejected,
    build_input_descriptor,
    sha256_bytes,
    validate_upload,
)
from harness.qualification import QualificationService
from release.governance import ReleaseGovernance
from storage.postgres import PostgresDatabase
from utils.auth import create_access_token, get_current_identity, verify_password
from utils.logger import logger
from utils.oidc import begin as begin_oidc
from utils.oidc import finish as finish_oidc
from utils.s3_utils import S3Utils
from utils.user_db import get_user
from webui import state as runtime
from webui.schemas import (
    H5AnnotationDecisionRequest,
    H5SnapshotDecisionRequest,
    H5SourceRevokeRequest,
    H5SourceSelectorRequest,
    H6PilotCreateRequest,
    H6PilotEvidenceRequest,
    H6QualificationCreateRequest,
    H6QualificationDecisionRequest,
    ReleaseAdvanceRequest,
    ReleaseObservationRequest,
    ReloadRequest,
    ReloadResponse,
    Token,
)

router = APIRouter()


@router.post("/api/jobs/full-cycle")
async def trigger_full_cycle(identity: dict = Depends(get_current_identity)):
    """The annotation bypass is intentionally closed by the H2 harness."""
    runtime._require_admin(identity)
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="full-cycle bypass is disabled; create a strict harness task instead",
    )


@router.get("/api/models/status")
async def model_status(identity: dict = Depends(get_current_identity)):
    """Return tenant-scoped active model evidence."""
    runtime._require_admin(identity)
    try:
        return runtime._adapter_runtime.model_status(identity)
    except (PermissionError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/models/reload", response_model=ReloadResponse)
async def reload_model(
    request: ReloadRequest | None = None, identity: dict = Depends(get_current_identity)
):
    """Load one explicitly selected, tenant-scoped promoted release."""
    runtime._require_admin(identity)
    request = request or ReloadRequest()
    try:
        # Run in executor as it might involve S3 downloads and model loading
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(
            None,
            runtime._adapter_runtime.check_and_reload_adapter,
            True,
            identity,
            request.release_id,
        )
        status = runtime._adapter_runtime.model_status(identity)
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


@router.post("/api/auth/login", response_model=Token)
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


@router.get("/api/auth/oidc/login")
async def oidc_login():
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=404, detail="OIDC login is disabled")
    authorization_url, _ = begin_oidc()
    return RedirectResponse(authorization_url)


@router.get("/api/auth/oidc/callback", response_model=Token)
async def oidc_callback(code: str, state: str):
    if AUTH_MODE != "oidc":
        raise HTTPException(status_code=404, detail="OIDC login is disabled")
    try:
        identity = finish_oidc(code, state)
    except PermissionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    access_token = create_access_token({"sub": identity["username"], **identity})
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/api/auth/me")
async def read_users_me(identity: dict = Depends(get_current_identity)):
    return identity


@router.get("/api/audit-events")
async def list_audit_events(identity: dict = Depends(get_current_identity)):
    runtime._require_admin(identity)
    try:
        return {"events": runtime.audit_log.list(identity)}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.post("/api/pilot-runs/document")
async def create_document_pilot_run(
    file: UploadFile = File(...),
    question: str = Form(...),
    acl: str = Form(""),
    expected_phrase: str = Form(""),
    identity: dict = Depends(get_current_identity),
):
    """Land one PDF/DOCX and create its durable H3 strict task."""
    runtime._require_admin(identity)
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
        source_uri = f"s3://{runtime.MINIO_BUCKET}/{raw_key}"
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
        raw_ref = f"raw:s3a://{runtime.MINIO_BUCKET}/{raw_prefix}"
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
                    "input_key": f"s3a://{runtime.MINIO_BUCKET}/{raw_prefix}",
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
        task = runtime.agent_runtime.create_task(
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
        task = await runtime.agent_runtime.run(task["task_id"], identity)
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


@router.post("/api/annotations/{annotation_id}/decision")
@router.post("/api/h5/annotations/{annotation_id}/decision")
async def decide_h5_annotation(
    annotation_id: str,
    request: H5AnnotationDecisionRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_reviewer(identity)
    try:
        EvaluationService(DATABASE_URL).review_annotation(
            identity,
            annotation_id,
            status=request.status,
            training_allowed=request.training_allowed,
            training_purpose=request.training_purpose,
            permission_version=request.permission_version,
            reason=request.reason,
            expected_response=request.expected_response,
            expected_citations=request.expected_citations,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"annotation_id": annotation_id, "status": request.status}


@router.post("/api/h5/source-impact")
async def h5_source_impact(
    request: H5SourceSelectorRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_reviewer(identity)
    try:
        return EvaluationService(DATABASE_URL).source_impact(
            identity, **request.model_dump(exclude_none=True)
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/h5/source-revoke")
async def h5_source_revoke(
    request: H5SourceRevokeRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_reviewer(identity)
    try:
        values = request.model_dump(exclude_none=True)
        reason = values.pop("reason")
        return EvaluationService(DATABASE_URL).revoke_source(identity, reason=reason, **values)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/api/qualifications")
@router.post("/api/h6/qualifications")
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


@router.get("/api/qualifications")
@router.get("/api/h6/qualifications")
async def list_h6_qualifications(identity: dict = Depends(get_current_identity)):
    return {"qualifications": QualificationService(DATABASE_URL).list(identity)}


@router.get("/api/qualifications/{qualification_id}")
@router.get("/api/h6/qualifications/{qualification_id}")
async def get_h6_qualification(
    qualification_id: str, identity: dict = Depends(get_current_identity)
):
    qualification = QualificationService(DATABASE_URL).get(identity, qualification_id)
    if qualification is None:
        raise HTTPException(status_code=404, detail="Qualification not found")
    return qualification


@router.post("/api/qualifications/{qualification_id}/decision")
@router.post("/api/h6/qualifications/{qualification_id}/decision")
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


@router.post("/api/h6/pilots")
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


@router.post("/api/h6/pilots/{pilot_id}/evidence")
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


@router.get("/api/h6/pilots/{pilot_id}")
async def get_h6_pilot(pilot_id: str, identity: dict = Depends(get_current_identity)):
    try:
        return PilotService(DATABASE_URL).status(identity, pilot_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/api/evaluations/{evaluation_id}")
@router.get("/api/h5/evaluations/{evaluation_id}")
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


@router.get("/api/annotations")
@router.get("/api/h5/annotations")
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


@router.get("/api/training-snapshots")
@router.get("/api/h5/training-snapshots")
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


@router.post("/api/training-snapshots/{snapshot_id}/decision")
@router.post("/api/h5/training-snapshots/{snapshot_id}/decision")
async def decide_h5_snapshot(
    snapshot_id: str,
    request: H5SnapshotDecisionRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_reviewer(identity)
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


@router.get("/api/adapters")
@router.get("/api/h5/adapters")
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


@router.get("/api/releases")
@router.get("/api/h5/releases")
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


@router.post("/api/h5/releases/{release_id}/advance")
async def advance_h5_release(
    release_id: str,
    request: ReleaseAdvanceRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_admin(identity)
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


@router.post("/api/h5/releases/{release_id}/observe")
async def observe_h5_release(
    release_id: str,
    request: ReleaseObservationRequest,
    identity: dict = Depends(get_current_identity),
):
    runtime._require_admin(identity)
    try:
        status_value = ReleaseGovernance(DATABASE_URL).observe(
            release_id, request.model_dump(exclude={"promote"}), identity, promote=request.promote
        )
    except (PermissionError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"release_id": release_id, "status": status_value}
