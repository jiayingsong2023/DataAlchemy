"""Web API request and response schemas."""

from typing import Any, Optional

from pydantic import BaseModel, Field


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


class H5AnnotationDecisionRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected|revoked)$")
    training_allowed: bool = False
    training_purpose: Optional[str] = None
    permission_version: Optional[str] = None
    reason: Optional[str] = None
    expected_response: Optional[str] = None
    expected_citations: list[dict[str, Any]] = Field(default_factory=list)


class H5SnapshotDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|revoke)$")
    reason: Optional[str] = None


class H5SourceSelectorRequest(BaseModel):
    source_version: Optional[str] = None
    source_acl_digest: Optional[str] = None
    permission_version: Optional[str] = None


class H5SourceRevokeRequest(H5SourceSelectorRequest):
    reason: str = Field(min_length=1, max_length=1000)


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
