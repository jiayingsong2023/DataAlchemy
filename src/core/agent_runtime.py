"""The single, durable PostgreSQL task runtime used by the agent harness."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from config import VERIFIER_DATABASE_URL
from core.evidence import EvidenceService
from core.jobs import JobService
from core.verifiers import ReadOnlyServices, VerificationResult, VerifierRegistry, default_verifiers
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
FINAL_STEP_ID = "00000000-0000-0000-0000-000000000000"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"), sort_keys=True)


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ReconciliationRequired(RuntimeError):
    """A side effect may have completed, so retrying would be unsafe."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[dict[str, Any]], Any | Awaitable[Any]]
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    roles: frozenset[str] = frozenset({"user", "admin"})
    timeout_seconds: float = 60.0
    requires_approval: bool = False
    idempotent: bool = False
    side_effecting: bool = False
    uses_identity: bool = False
    max_result_bytes: int = 65_536
    max_calls_per_minute: int = 60
    max_retries: int = 0
    sensitive_fields: frozenset[str] = frozenset()
    version: int = 1
    scope_resolver: Callable[[dict[str, Any], dict[str, str]], list[str]] | None = None
    result_validator: Callable[[dict[str, Any]], None] | None = None
    expected_artifacts: frozenset[tuple[str, str]] = frozenset()
    result_sensitivity: dict[str, str] = field(default_factory=dict)
    blocked_reason: str | None = None
    execution: str = "inline"
    job_kind: str | None = None

    @property
    def contract_digest(self) -> str:
        return _digest(
            {
                "name": self.name,
                "version": self.version,
                "schema": self.schema,
                "roles": sorted(self.roles),
                "side_effecting": self.side_effecting,
                "expected_artifacts": sorted(self.expected_artifacts),
                "result_sensitivity": self.result_sensitivity,
                "blocked_reason": self.blocked_reason,
                "execution": self.execution,
                "job_kind": self.job_kind,
            }
        )


class ToolRegistry:
    """One validation and authorization point for registered tools."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} already registered")
        if spec.max_calls_per_minute < 1:
            raise ValueError("Tool rate limit must be positive")
        if spec.max_retries and not spec.idempotent:
            raise ValueError("Only idempotent tools may declare retries")
        if spec.side_effecting and not spec.idempotent:
            raise ValueError("H0 side-effecting tools must be idempotent")
        if spec.version < 1:
            raise ValueError("Tool version must be positive")
        if spec.execution not in {"inline", "kubernetes_job"}:
            raise ValueError("Tool execution must be inline or kubernetes_job")
        if (spec.execution == "kubernetes_job") != (spec.job_kind is not None):
            raise ValueError("Kubernetes jobs require exactly one job_kind")
        if any(
            level not in {"public", "internal", "secret"}
            for level in spec.result_sensitivity.values()
        ):
            raise ValueError("Tool result sensitivity must be public, internal, or secret")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ValueError(f"Unknown tool: {name}") from error

    def sensitivity(self, name: str) -> dict[str, str]:
        return self.get(name).result_sensitivity

    def validate(self, spec: ToolSpec, arguments: dict[str, Any], role: str) -> None:
        if role not in spec.roles:
            raise PermissionError(f"Role {role!r} cannot call {spec.name!r}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        schema = spec.schema
        if schema.get("type", "object") != "object":
            raise ValueError("Only object tool schemas are supported")
        required = set(schema.get("required", []))
        if missing := required - arguments.keys():
            raise ValueError(f"Missing tool arguments: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and (
            extra := arguments.keys() - properties.keys()
        ):
            raise ValueError(f"Unexpected tool arguments: {', '.join(sorted(extra))}")
        for name, value in arguments.items():
            expected = properties.get(name, {}).get("type")
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"Tool argument {name!r} must be a string")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"Tool argument {name!r} must be an integer")
            if expected == "boolean" and not isinstance(value, bool):
                raise ValueError(f"Tool argument {name!r} must be a boolean")


class AgentRuntime:
    """Plan → Act → Observe → Replan with PostgreSQL task contracts and leases."""

    terminal_states = frozenset(
        {
            "succeeded",
            "failed",
            "cancelled",
            "reconciliation_required",
            "verification_failed",
            "verification_blocked",
        }
    )
    stop_states = terminal_states | frozenset(
        {"paused", "waiting_approval", "awaiting_verification", "evidence_pending", "waiting_job"}
    )

    def __init__(
        self,
        database_url: str,
        tools: ToolRegistry,
        verifiers: VerifierRegistry | None = None,
        evidence: EvidenceService | None = None,
        jobs: JobService | None = None,
    ):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)
        self.tools = tools
        self.verifiers = verifiers or default_verifiers()
        self.verifier_database_url = VERIFIER_DATABASE_URL or database_url
        self.evidence = evidence
        self.jobs = jobs
        self._rate_windows: dict[tuple[str, str], list[float]] = {}

    def _allow_call(self, spec: ToolSpec, identity: dict[str, str]) -> None:
        key = (identity["tenant_id"], spec.name)
        now = time.monotonic()
        window = [item for item in self._rate_windows.get(key, []) if now - item < 60]
        if len(window) >= spec.max_calls_per_minute:
            raise RuntimeError(f"Tool {spec.name!r} rate limit exceeded")
        window.append(now)
        self._rate_windows[key] = window

    @staticmethod
    def _redact(value: Any, fields: frozenset[str]) -> Any:
        if isinstance(value, dict):
            return {
                key: "***" if key in fields else AgentRuntime._redact(item, fields)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [AgentRuntime._redact(item, fields) for item in value]
        return value

    @staticmethod
    def _plan_hash(plan: list[dict[str, Any]]) -> str:
        return hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()

    def _safe_plan(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **step,
                "arguments": self._redact(
                    step.get("arguments", {}), self.tools.get(step["tool"]).sensitive_fields
                ),
            }
            for step in plan
        ]

    def _criteria(self, criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
        frozen: list[dict[str, Any]] = []
        for raw in criteria:
            criterion_id = raw.get("criterion_id")
            phase = raw.get("phase", "after_step")
            if not isinstance(criterion_id, str) or not criterion_id:
                raise ValueError("Each strict criterion needs criterion_id")
            if phase not in {"after_step", "final"}:
                raise ValueError("Criterion phase must be after_step or final")
            name, version = raw.get("verifier"), raw.get("version")
            if not isinstance(name, str) or not isinstance(version, int):
                raise ValueError("Each strict criterion needs verifier and version")
            if not isinstance(raw.get("parameters", {}), dict) or not isinstance(
                raw.get("required", True), bool
            ):
                raise ValueError(
                    "Criterion parameters must be an object and required must be boolean"
                )
            verifier = self.verifiers.get(name, version)
            frozen.append(
                {
                    "criterion_id": criterion_id,
                    "verifier": name,
                    "version": version,
                    "contract_digest": verifier.contract_digest,
                    "parameters": raw.get("parameters", {}),
                    "phase": phase,
                    "required": raw.get("required", True),
                }
            )
        if not frozen or len({item["criterion_id"] for item in frozen}) != len(frozen):
            raise ValueError("Strict criteria must be non-empty with unique criterion_id")
        return frozen

    def _normalize_plan(  # noqa: C901
        self,
        identity: dict[str, str],
        plan: list[dict[str, Any]],
        run_id: str,
        execution_mode: str,
        allowed_tools: set[str] | None = None,
        allowed_scope: set[str] | None = None,
        plan_version: int = 1,
        criteria: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not plan:
            raise ValueError("Task plan must not be empty")
        normalized: list[dict[str, Any]] = []
        for raw in plan:
            tool_name = raw.get("tool", "")
            spec = self.tools.get(tool_name)
            arguments = raw.get("arguments", {})
            self.tools.validate(spec, arguments, identity["role"])
            if allowed_tools is not None and tool_name not in allowed_tools:
                raise PermissionError(f"Tool {tool_name!r} is outside the task contract")
            if execution_mode == "legacy" and spec.side_effecting:
                raise ValueError("Legacy tasks cannot call side-effecting tools")
            if spec.side_effecting and not spec.idempotent:
                raise ValueError(f"Side-effecting tool {tool_name!r} must be idempotent")
            if spec.blocked_reason:
                raise ValueError(f"Tool {tool_name!r} is blocked: {spec.blocked_reason}")
            scope_refs = raw.get("scope_refs", [])
            if execution_mode == "strict":
                if not isinstance(scope_refs, list) or not all(
                    isinstance(item, str) and item for item in scope_refs
                ):
                    raise ValueError("Strict task steps need a list of scope_refs")
                if allowed_scope is not None and not set(scope_refs) <= allowed_scope:
                    raise PermissionError("Task step expands the task data scope")
            elif scope_refs:
                raise ValueError("Legacy task steps cannot declare scope_refs")
            verifier_refs = raw.get("verifier_refs", [])
            if execution_mode == "strict":
                if not isinstance(verifier_refs, list) or not all(
                    isinstance(item, str) and item for item in verifier_refs
                ):
                    raise ValueError("Strict task verifier_refs must be a list of criterion IDs")
                known = {item["criterion_id"]: item for item in criteria or []}
                if not set(verifier_refs) <= set(known):
                    raise ValueError("Task step references an unknown criterion")
                if spec.side_effecting and not any(
                    known[item]["required"] and known[item]["phase"] == "after_step"
                    for item in verifier_refs
                ):
                    raise ValueError(
                        "Side-effecting strict steps need a required after_step verifier"
                    )
            elif verifier_refs:
                raise ValueError("Legacy task steps cannot declare verifier_refs")
            step_id = raw.get("step_id") or str(uuid.uuid4())
            if not isinstance(step_id, str):
                raise ValueError("step_id must be a string")
            key = f"{run_id}:{step_id}"
            normalized.append(
                {
                    "step_id": step_id,
                    "tool": tool_name,
                    "arguments": arguments,
                    "scope_refs": scope_refs,
                    "verifier_refs": verifier_refs,
                    "idempotency_key": key,
                    "tool_version": spec.version,
                    "tool_contract_digest": spec.contract_digest,
                    "created_in_plan_version": raw.get("created_in_plan_version", plan_version),
                }
            )
        if len({step["step_id"] for step in normalized}) != len(normalized):
            raise ValueError("Task plan step_id values must be unique")
        return normalized

    @staticmethod
    def _validate_strict_spec(task_spec: dict[str, Any], max_steps: int) -> dict[str, Any]:
        criteria = task_spec.get("success_criteria")
        scope = task_spec.get("data_scope")
        limits = task_spec.get("limits")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("Strict tasks require success_criteria")
        if not isinstance(scope, dict) or not isinstance(scope.get("source_refs"), list):
            raise ValueError("Strict tasks require data_scope.source_refs")
        if not all(isinstance(item, str) and item for item in scope["source_refs"]):
            raise ValueError("data_scope.source_refs must contain strings")
        if not isinstance(limits, dict) or set(limits) - {"max_steps", "deadline_seconds"}:
            raise ValueError("Strict task limits only support max_steps and deadline_seconds")
        if limits.get("max_steps") != max_steps or not isinstance(
            limits.get("deadline_seconds"), int
        ):
            raise ValueError(
                "Strict task limits must contain matching max_steps and deadline_seconds"
            )
        if limits["deadline_seconds"] < 1:
            raise ValueError("deadline_seconds must be positive")
        return task_spec

    def create_task(
        self,
        identity: dict[str, str],
        goal: str,
        plan: list[dict[str, Any]],
        max_steps: int = 8,
        budget: dict[str, Any] | None = None,
        *,
        execution_mode: str = "legacy",
        task_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("Task goal cannot be empty")
        if max_steps < 1 or len(plan) > max_steps:
            raise ValueError("Task plan must contain no more than max_steps steps")
        if execution_mode not in {"legacy", "strict"}:
            raise ValueError("execution_mode must be legacy or strict")
        if execution_mode == "legacy" and len(plan) != 1:
            raise ValueError("Legacy tasks must contain exactly one step")
        task_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = _now()
        if execution_mode == "strict":
            if not isinstance(task_spec, dict):
                raise ValueError("Strict tasks require task_spec")
            self._validate_strict_spec(task_spec, max_steps)
            criteria = self._criteria(task_spec["success_criteria"])
            scope = set(task_spec["data_scope"]["source_refs"])
            normalized = self._normalize_plan(
                identity, plan, run_id, execution_mode, allowed_scope=scope, criteria=criteria
            )
            after_step = {
                item["criterion_id"]
                for item in criteria
                if item["phase"] == "after_step" and item["required"]
            }
            reference_counts = {
                criterion_id: sum(criterion_id in step["verifier_refs"] for step in normalized)
                for criterion_id in after_step
            }
            if any(count != 1 for count in reference_counts.values()):
                raise ValueError(
                    "Each required after_step criterion must be referenced by one step"
                )
            frozen_spec = {
                "schema_version": 2,
                "execution_mode": "strict",
                "success_criteria": criteria,
                "data_scope": task_spec["data_scope"],
                "allowed_tools": sorted({step["tool"] for step in normalized}),
                "tool_contracts": {
                    step["tool"]: {
                        "version": step["tool_version"],
                        "contract_digest": step["tool_contract_digest"],
                    }
                    for step in normalized
                },
                "limits": task_spec["limits"],
                "created_by": identity["username"],
                "created_at": now,
            }
        else:
            normalized = self._normalize_plan(identity, plan, run_id, execution_mode)
            frozen_spec = {"schema_version": 1, "execution_mode": "legacy", "legacy": True}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_tasks "
                    "(task_id, run_id, tenant_id, owner, role, goal, state, plan_json, max_steps, "
                    "budget_json, task_spec_json, plan_version, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'created', %s::jsonb, %s, %s::jsonb, "
                    "%s::jsonb, 1, %s, %s)",
                    (
                        task_id,
                        run_id,
                        identity["tenant_id"],
                        identity["username"],
                        identity["role"],
                        goal,
                        _json(normalized),
                        max_steps,
                        _json(budget or {}),
                        _json(frozen_spec),
                        now,
                        now,
                    ),
                )
                self._event(
                    cursor,
                    task_id,
                    identity["tenant_id"],
                    "planned",
                    {
                        "goal": goal,
                        "run_id": run_id,
                        "plan_version": 1,
                        "task_spec_schema_version": frozen_spec["schema_version"],
                        "plan_hash": self._plan_hash(normalized),
                        "plan": self._safe_plan(normalized),
                    },
                )
        task = self.get_task(task_id, identity)
        self.audit.record(
            identity, "task.planned", "task", resource_id=task_id, correlation_id=run_id
        )
        return task

    @staticmethod
    def _event(
        cursor: Any, task_id: str, tenant_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        cursor.execute(
            "INSERT INTO agent_events "
            "(event_id, task_id, tenant_id, event_type, payload_json, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
            (str(uuid.uuid4()), task_id, tenant_id, event_type, _json(payload), _now()),
        )

    @staticmethod
    def _row_to_task(row: dict[str, Any]) -> dict[str, Any]:
        task = dict(row)
        for key in ("plan_json", "budget_json", "approval_json", "task_spec_json"):
            task[key.removesuffix("_json")] = _decode(task.pop(key))
        for key in ("task_id", "run_id"):
            if task.get(key) is not None:
                task[key] = str(task[key])
        return task

    def get_task(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM agent_tasks WHERE task_id = %s", (task_id,))
                row = cursor.fetchone()
        if row is None:
            raise PermissionError("Task not found")
        return self._row_to_task(row)

    def list_tasks(self, identity: dict[str, str]) -> list[dict[str, Any]]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM agent_tasks ORDER BY created_at DESC")
                return [self._row_to_task(row) for row in cursor.fetchall()]

    def events(self, task_id: str, identity: dict[str, str]) -> list[dict[str, Any]]:
        self.get_task(task_id, identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, event_type, payload_json, occurred_at FROM agent_events "
                    "WHERE task_id = %s ORDER BY occurred_at, event_id",
                    (task_id,),
                )
                return [
                    {
                        **row,
                        "event_id": str(row["event_id"]),
                        "payload": _decode(row.pop("payload_json")),
                    }
                    for row in cursor.fetchall()
                ]

    def verifications(self, task_id: str, identity: dict[str, str]) -> list[dict[str, Any]]:
        self.get_task(task_id, identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT verification_id, step_id, criterion_id, verifier, verifier_version, "
                    "attempt, status, tool_result_digest, input_digest, error_code, summary_json, "
                    "started_at, completed_at FROM agent_step_verifications "
                    "WHERE task_id = %s ORDER BY completed_at, attempt",
                    (task_id,),
                )
                return [
                    {
                        **row,
                        "verification_id": str(row["verification_id"]),
                        "step_id": str(row["step_id"]),
                        "summary": _decode(row.pop("summary_json")),
                    }
                    for row in cursor.fetchall()
                ]

    def tool_runs(self, task_id: str, identity: dict[str, str]) -> list[dict[str, Any]]:
        """Return redacted terminal tool facts for the H3 run projection."""
        self.get_task(task_id, identity)
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tool_name, step_id, plan_version, attempt, state, started_at, completed_at, result_json "
                    "FROM agent_tool_runs WHERE task_id = %s ORDER BY started_at, step_id",
                    (task_id,),
                )
                runs = []
                for row in cursor.fetchall():
                    result = _decode(row.pop("result_json")) or {}
                    runs.append(
                        {
                            **row,
                            "step_id": str(row["step_id"]) if row.get("step_id") else None,
                            "result": {
                                "status": result.get("status"),
                                "artifacts": result.get("artifacts", []),
                                "metrics": result.get("metrics", {}),
                                "output": {
                                    key: value
                                    for key, value in result.get("output", {}).items()
                                    if key not in {"answer", "prompt", "content"}
                                },
                                "failure": result.get("failure"),
                            },
                        }
                    )
                return runs

    def evidence_status(self, task_id: str, identity: dict[str, str]) -> dict[str, Any] | None:
        task = self.get_task(task_id, identity)
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM run_manifests WHERE run_id = %s", (task["run_id"],))
                row = cursor.fetchone()
        if row is None:
            return None
        for key in ("run_id", "task_id"):
            row[key] = str(row[key])
        return row

    def reconcile_evidence(
        self, task_id: str, identity: dict[str, str], expected_version: int
    ) -> dict[str, Any]:
        if self.evidence is None:
            raise RuntimeError("Evidence publishing is not configured")
        task = self.get_task(task_id, identity)
        if task["state"] != "evidence_pending":
            raise ValueError("Only evidence-pending tasks can be reconciled")
        if task["version"] != expected_version:
            raise RuntimeError("Task state changed; reload before reconciling")
        published = self.evidence.reconcile(task, identity)
        changed = self._transition(
            task_id,
            identity,
            "succeeded",
            "evidence_published",
            {"run_id": task["run_id"], **published},
            expected_version=expected_version,
            finish_reason="verified_evidence_published",
        )
        return changed

    def delete_evidence(self, task_id: str, identity: dict[str, str]) -> None:
        if self.evidence is None:
            raise RuntimeError("Evidence publishing is not configured")
        if identity["role"] != "admin":
            raise PermissionError("Administrator role required")
        self.evidence.delete_manifest(self.get_task(task_id, identity), identity)

    async def reconcile_job(
        self, task_id: str, identity: dict[str, str], expected_version: int
    ) -> dict[str, Any]:
        if self.jobs is None:
            raise RuntimeError("Kubernetes job execution is not configured")
        task = self.get_task(task_id, identity)
        if task["state"] not in {"waiting_job", "cancelling"}:
            raise ValueError("Task is not waiting for a job")
        if task["version"] != expected_version:
            raise RuntimeError("Task state changed; reload before reconciling")
        job = self.jobs.for_task(task, identity)
        if job is None:
            raise RuntimeError("Task job handle is missing")
        observation = self.jobs.reconcile(job, identity)
        if observation.state == "running":
            return self.get_task(task_id, identity)
        if observation.state == "cancelled":
            return self._transition(
                task_id,
                identity,
                "cancelled",
                "job_cancelled",
                {"job_id": job["job_id"]},
                expected_version=expected_version,
                finish_reason="cancelled",
            )
        if observation.state in {"orphaned", "reconciliation_required"}:
            return self._transition(
                task_id,
                identity,
                "reconciliation_required",
                "job_reconciliation_required",
                {"job_id": job["job_id"], "error_code": observation.error_code},
                expected_version=expected_version,
                finish_reason=observation.error_code,
            )
        if observation.state == "failed" or observation.result is None:
            return self._fail(
                task_id, identity, observation.error_code or "job_result_missing", expected_version
            )
        step = task["plan"][task["current_step"]]
        spec = self.tools.get(step["tool"])
        result = self._tool_result(task, step, spec, observation.result)
        self._finish_tool_run(task, spec, step, "succeeded", _json(result))
        resumed = self._transition(
            task_id,
            identity,
            "created",
            "job_result_materialized",
            {"job_id": job["job_id"], "result_digest": _digest(result)},
            expected_version=expected_version,
            finish_reason=None,
        )
        return await self.run(task_id, identity) if resumed["state"] == "created" else resumed

    def _transition(
        self,
        task_id: str,
        identity: dict[str, str],
        state: str,
        event: str,
        payload: dict[str, Any],
        *,
        expected_version: int | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        if expected_version is None:
            expected_version = self.get_task(task_id, identity)["version"]
        assignments = ["state = %s", "updated_at = %s", "version = version + 1"]
        values: list[Any] = [state, _now()]
        for column, value in updates.items():
            assignments.append(f"{column} = %s")
            values.append(_json(value) if column.endswith("_json") else value)
        values.extend([task_id, expected_version])
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE agent_tasks SET {', '.join(assignments)} "
                    "WHERE task_id = %s AND version = %s",
                    values,
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Task state changed; reload before retrying")
                self._event(cursor, task_id, identity["tenant_id"], event, payload)
        return self.get_task(task_id, identity)

    def pause(
        self, task_id: str, identity: dict[str, str], expected_version: int | None = None
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states | {"awaiting_verification"}:
            raise ValueError("Terminal tasks cannot be paused")
        if task["state"] in {"waiting_job", "cancelling"}:
            raise ValueError("A submitted job may only be cancelled")
        state = "pausing" if task.get("lease_owner") else "paused"
        return self._transition(
            task_id,
            identity,
            state,
            "control_requested",
            {"control": "pause", "by": identity["username"]},
            expected_version=expected_version or task["version"],
            pause_requested=state == "pausing",
        )

    def cancel(
        self, task_id: str, identity: dict[str, str], expected_version: int | None = None
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states | {"awaiting_verification"}:
            raise ValueError("Terminal tasks cannot be cancelled")
        state = (
            "cancelling"
            if task.get("lease_owner") or task["state"] == "waiting_job"
            else "cancelled"
        )
        changed = self._transition(
            task_id,
            identity,
            state,
            "control_requested",
            {"control": "cancel", "by": identity["username"]},
            expected_version=expected_version or task["version"],
            cancel_requested=state == "cancelling",
            finish_reason="cancelled" if state == "cancelled" else None,
        )
        if state == "cancelling" and task["state"] == "waiting_job" and self.jobs is not None:
            self.jobs.request_cancel(changed, identity)
        return changed

    def resume(
        self, task_id: str, identity: dict[str, str], expected_version: int | None = None
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "paused":
            raise ValueError("Only paused tasks can be resumed")
        return self._transition(
            task_id,
            identity,
            "created",
            "resumed",
            {"by": identity["username"]},
            expected_version=expected_version or task["version"],
            pause_requested=False,
            cancel_requested=False,
            finish_reason=None,
        )

    def retry(
        self, task_id: str, identity: dict[str, str], expected_version: int | None = None
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "failed":
            raise ValueError("Only failed tasks can be retried")
        return self._transition(
            task_id,
            identity,
            "created",
            "retried",
            {"by": identity["username"]},
            expected_version=expected_version or task["version"],
            finish_reason=None,
            pause_requested=False,
            cancel_requested=False,
        )

    def retry_verification(
        self, task_id: str, identity: dict[str, str], expected_version: int
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "verification_blocked":
            raise ValueError("Only blocked verification can be retried")
        return self._transition(
            task_id,
            identity,
            "created",
            "verification_retry_requested",
            {"by": identity["username"]},
            expected_version=expected_version,
            finish_reason=None,
        )

    def approve(
        self,
        task_id: str,
        identity: dict[str, str],
        approved: bool,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "waiting_approval" or not task["approval"]:
            raise ValueError("Task is not waiting for approval")
        approval = {**task["approval"], "approved": approved, "approved_by": identity["username"]}
        task = self._transition(
            task_id,
            identity,
            "created" if approved else "cancelled",
            "approval_granted" if approved else "approval_rejected",
            {"by": identity["username"]},
            expected_version=expected_version or task["version"],
            approval_json=approval,
            finish_reason=None if approved else "approval_rejected",
        )
        self.audit.record(
            identity,
            "task.approval",
            "task",
            outcome="allowed" if approved else "denied",
            resource_id=task_id,
            correlation_id=task["run_id"],
        )
        return task

    def replan(
        self,
        task_id: str,
        identity: dict[str, str],
        remaining_steps: list[dict[str, Any]],
        reason: str,
        expected_version: int,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("Replan reason is required")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM agent_tasks WHERE task_id = %s FOR UPDATE", (task_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Task not found")
                task = self._row_to_task(row)
                if task["state"] not in {
                    "paused",
                    "waiting_approval",
                    "failed",
                    "verification_failed",
                    "verification_blocked",
                }:
                    raise ValueError("Replan requires a safe stopped task")
                if (
                    task["lease_owner"]
                    and task["lease_expires_at"]
                    and task["lease_expires_at"] > datetime.now(timezone.utc)
                ):
                    raise RuntimeError("Task lease is still active")
                if task["version"] != expected_version:
                    raise RuntimeError("Task state changed; reload before replanning")
                if task["task_spec"].get("execution_mode") != "strict":
                    raise ValueError("Only strict tasks can be replanned")
                next_version = task["plan_version"] + 1
                allowed_tools = set(task["task_spec"]["allowed_tools"])
                allowed_scope = set(task["task_spec"]["data_scope"]["source_refs"])
                suffix = self._normalize_plan(
                    identity,
                    remaining_steps,
                    task["run_id"],
                    "strict",
                    allowed_tools,
                    allowed_scope,
                    next_version,
                    task["task_spec"].get("success_criteria", []),
                )
                criteria = {
                    item["criterion_id"]: item
                    for item in task["task_spec"].get("success_criteria", [])
                }
                new_plan = task["plan"][: task["current_step"]] + suffix
                for criterion in criteria.values():
                    if criterion["phase"] == "after_step" and criterion["required"]:
                        count = sum(
                            criterion["criterion_id"] in step.get("verifier_refs", [])
                            for step in new_plan
                        )
                        if count != 1:
                            raise ValueError(
                                "Replan must retain each required after_step criterion once"
                            )
                if len(new_plan) > task["max_steps"]:
                    raise ValueError("Replanned task exceeds max_steps")
                cursor.execute(
                    "UPDATE agent_tasks SET plan_json = %s::jsonb, plan_version = %s, state = 'paused', "
                    "approval_json = NULL, pause_requested = false, cancel_requested = false, finish_reason = NULL, "
                    "lease_owner = NULL, lease_expires_at = NULL, updated_at = %s, version = version + 1 "
                    "WHERE task_id = %s AND version = %s",
                    (_json(new_plan), next_version, _now(), task_id, expected_version),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Task state changed; reload before replanning")
                self._event(
                    cursor,
                    task_id,
                    identity["tenant_id"],
                    "replanned",
                    {
                        "by": identity["username"],
                        "reason": reason,
                        "run_id": task["run_id"],
                        "old_plan_version": task["plan_version"],
                        "new_plan_version": next_version,
                        "current_step": task["current_step"],
                        "old_plan_hash": self._plan_hash(task["plan"]),
                        "new_plan_hash": self._plan_hash(new_plan),
                        "remaining_plan": self._safe_plan(suffix),
                    },
                )
        return self.get_task(task_id, identity)

    def _acquire_lease(
        self, task_id: str, identity: dict[str, str], worker_id: str
    ) -> dict[str, Any] | None:
        task = self.get_task(task_id, identity)
        if task["state"] in self.stop_states:
            return None
        recovered = bool(task.get("lease_owner"))
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tasks SET lease_owner = %s, lease_expires_at = now() + (%s * interval '1 second'), "
                    "heartbeat_at = now(), state = CASE WHEN state = 'created' THEN 'running' ELSE state END, "
                    "updated_at = now(), version = version + 1 "
                    "WHERE task_id = %s AND version = %s "
                    "AND state NOT IN ('succeeded', 'failed', 'cancelled', 'reconciliation_required', 'paused', "
                    "'waiting_approval', 'awaiting_verification', 'evidence_pending') "
                    "AND (lease_owner IS NULL OR lease_expires_at <= now() OR lease_owner = %s) RETURNING *",
                    (worker_id, LEASE_SECONDS, task_id, task["version"], worker_id),
                )
                row = cursor.fetchone()
                if row is None:
                    latest = self.get_task(task_id, identity)
                    if latest["state"] in self.stop_states:
                        return None
                    raise RuntimeError("Task is already running")
                leased = self._row_to_task(row)
                self._event(
                    cursor,
                    task_id,
                    identity["tenant_id"],
                    "lease_recovered" if recovered else "lease_acquired",
                    {
                        "worker_id": worker_id,
                        "run_id": leased["run_id"],
                        "plan_version": leased["plan_version"],
                    },
                )
                if task["state"] == "created":
                    self._event(
                        cursor,
                        task_id,
                        identity["tenant_id"],
                        "started",
                        {"by": identity["username"]},
                    )
        return leased

    def _heartbeat(self, task_id: str, identity: dict[str, str], worker_id: str) -> bool:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tasks SET heartbeat_at = now(), lease_expires_at = now() + (%s * interval '1 second') "
                    "WHERE task_id = %s AND lease_owner = %s",
                    (LEASE_SECONDS, task_id, worker_id),
                )
                return cursor.rowcount == 1

    def _release_lease(self, task_id: str, identity: dict[str, str], worker_id: str) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tasks SET lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE task_id = %s AND lease_owner = %s",
                    (task_id, worker_id),
                )

    async def _heartbeats(self, task_id: str, identity: dict[str, str], worker_id: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not await asyncio.to_thread(self._heartbeat, task_id, identity, worker_id):
                return

    def _safe_control(
        self, task: dict[str, Any], identity: dict[str, str]
    ) -> dict[str, Any] | None:
        if task["cancel_requested"] or task["state"] == "cancelling":
            return self._transition(
                task["task_id"],
                identity,
                "cancelled",
                "cancelled",
                {"by": identity["username"]},
                expected_version=task["version"],
                cancel_requested=False,
                pause_requested=False,
                finish_reason="cancelled",
            )
        if task["pause_requested"] or task["state"] == "pausing":
            return self._transition(
                task["task_id"],
                identity,
                "paused",
                "paused",
                {"by": identity["username"]},
                expected_version=task["version"],
                pause_requested=False,
            )
        return None

    def _deadline_exceeded(self, task: dict[str, Any]) -> bool:
        limits = task["task_spec"].get("limits", {})
        deadline = limits.get("deadline_seconds")
        if deadline is None:
            return False
        created = task["created_at"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > created + timedelta(seconds=deadline)

    def _validate_step_contract(
        self, task: dict[str, Any], step: dict[str, Any], spec: ToolSpec, identity: dict[str, str]
    ) -> None:
        if task["task_spec"].get("execution_mode") != "strict":
            return
        frozen = task["task_spec"].get("tool_contracts", {}).get(spec.name)
        if not frozen or frozen != {
            "version": spec.version,
            "contract_digest": spec.contract_digest,
        }:
            raise RuntimeError(f"Tool contract drift: {spec.name}")
        resolved = (
            spec.scope_resolver(step.get("arguments", {}), identity) if spec.scope_resolver else []
        )
        if sorted(resolved) != sorted(step.get("scope_refs", [])):
            raise PermissionError("Tool scope does not match the frozen task scope")

    def _previous_artifacts(
        self, task: dict[str, Any], identity: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Return prior terminal artifacts for run-scoped H3 handoff."""
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT result_json FROM agent_tool_runs WHERE task_id = %s AND state = 'succeeded' "
                    "ORDER BY completed_at, step_id",
                    (task["task_id"],),
                )
                artifacts: list[dict[str, Any]] = []
                for row in cursor.fetchall():
                    result = _decode(row["result_json"])
                    if isinstance(result, dict) and isinstance(result.get("artifacts"), list):
                        artifacts.extend(result["artifacts"])
                return artifacts

    def _tool_result(
        self, task: dict[str, Any], step: dict[str, Any], spec: ToolSpec, payload: Any
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"Tool {spec.name!r} must return an object payload")
        if spec.result_validator:
            spec.result_validator(payload)
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError("Tool artifacts must be a list")
        if spec.expected_artifacts and not artifacts:
            raise ValueError("Tool result is missing required artifacts")
        for artifact in artifacts:
            if (
                not isinstance(artifact, dict)
                or not {"store", "kind", "id", "sha256"} <= artifact.keys()
            ):
                raise ValueError("Tool artifact is incomplete")
            if not isinstance(artifact["sha256"], str) or len(artifact["sha256"]) != 64:
                raise ValueError("Tool artifact hash must be SHA-256")
            if (
                spec.expected_artifacts
                and (artifact["store"], artifact["kind"]) not in spec.expected_artifacts
            ):
                raise ValueError("Tool artifact is outside the registered contract")
        observed = payload.get("observed_scope", step.get("scope_refs", []))
        if not isinstance(observed, list) or not set(observed) <= set(step.get("scope_refs", [])):
            raise PermissionError("Tool observed scope exceeds the task scope")
        output = {
            key: value
            for key, value in payload.items()
            if key
            not in {"artifacts", "metrics", "operation_ref", "log_ref", "observed_scope", "status"}
        }
        return {
            "schema_version": 1,
            "status": "succeeded",
            "tool": {
                "name": spec.name,
                "version": spec.version,
                "contract_digest": spec.contract_digest,
            },
            "input_refs": step.get("scope_refs", []),
            "observed_scope": observed,
            "output": output,
            "artifacts": artifacts,
            "metrics": payload.get("metrics", {}),
            "operation_ref": payload.get("operation_ref"),
            "log_ref": payload.get("log_ref"),
            "failure": None,
            "next_action": "verify",
            "recorded_at": _now(),
        }

    def _failed_tool_result(self, spec: ToolSpec, step: dict[str, Any], code: str) -> str:
        return _json(
            {
                "schema_version": 1,
                "status": "failed",
                "tool": {
                    "name": spec.name,
                    "version": spec.version,
                    "contract_digest": spec.contract_digest,
                },
                "input_refs": step.get("scope_refs", []),
                "artifacts": [],
                "failure": {
                    "category": "tool",
                    "code": code,
                    "redacted_message": "tool execution failed",
                },
                "recorded_at": _now(),
            }
        )

    async def _verify_criterion(
        self,
        task: dict[str, Any],
        step: dict[str, Any],
        criterion: dict[str, Any],
        result: dict[str, Any],
    ) -> VerificationResult:
        verifier = self.verifiers.get(criterion["verifier"], criterion["version"])
        if criterion["contract_digest"] != verifier.contract_digest:
            raise RuntimeError(f"Verifier contract drift: {criterion['criterion_id']}")
        services = ReadOnlyServices(
            self.verifier_database_url,
            {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]},
        )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(verifier.handler, criterion, task, result, services),
                timeout=verifier.timeout_seconds,
            )
        except TimeoutError:
            return VerificationResult("blocked", {}, "verifier_timeout")
        except Exception:
            return VerificationResult("blocked", {}, "verifier_unavailable")

    def _record_verification(
        self,
        task: dict[str, Any],
        step_id: str,
        criterion: dict[str, Any],
        result: dict[str, Any],
        outcome: VerificationResult,
    ) -> None:
        identity = {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(max(attempt), 0) + 1 AS attempt FROM agent_step_verifications "
                    "WHERE task_id = %s AND step_id = %s AND criterion_id = %s",
                    (task["task_id"], step_id, criterion["criterion_id"]),
                )
                attempt = cursor.fetchone()["attempt"]
                input_digest = _digest(
                    {
                        "task_id": task["task_id"],
                        "step_id": step_id,
                        "criterion": criterion,
                        "tool_result": _digest(result),
                    }
                )
                cursor.execute(
                    "INSERT INTO agent_step_verifications "
                    "(verification_id, tenant_id, task_id, step_id, criterion_id, verifier, verifier_version, "
                    "verifier_contract_digest, attempt, status, tool_result_digest, input_digest, error_code, summary_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        str(uuid.uuid4()),
                        task["tenant_id"],
                        task["task_id"],
                        step_id,
                        criterion["criterion_id"],
                        criterion["verifier"],
                        criterion["version"],
                        criterion["contract_digest"],
                        attempt,
                        outcome.status,
                        _digest(result),
                        input_digest,
                        outcome.error_code,
                        _json(outcome.summary),
                    ),
                )
                self._event(
                    cursor,
                    task["task_id"],
                    task["tenant_id"],
                    f"verification_{outcome.status}",
                    {
                        "run_id": task["run_id"],
                        "step_id": step_id,
                        "criterion_id": criterion["criterion_id"],
                        "verifier": criterion["verifier"],
                        "attempt": attempt,
                        "error_code": outcome.error_code,
                    },
                )

    async def _verify_step(
        self, task: dict[str, Any], step: dict[str, Any], result: dict[str, Any]
    ) -> VerificationResult:
        criteria = {
            item["criterion_id"]: item for item in task["task_spec"].get("success_criteria", [])
        }
        outcomes: list[VerificationResult] = []
        for criterion_id in step.get("verifier_refs", []):
            criterion = criteria[criterion_id]
            outcome = await self._verify_criterion(task, step, criterion, result)
            self._record_verification(task, step["step_id"], criterion, result, outcome)
            outcomes.append(outcome)
            if criterion["required"] and outcome.status != "passed":
                return outcome
        return VerificationResult("passed", {"verified": len(outcomes)})

    async def _verify_final(self, task: dict[str, Any]) -> VerificationResult:
        refs = [
            item["criterion_id"]
            for item in task["task_spec"].get("success_criteria", [])
            if item["phase"] == "final"
        ]
        result = {
            "schema_version": 1,
            "status": "succeeded",
            "tool": {"name": "final", "version": 1, "contract_digest": "final"},
            "input_refs": [],
            "observed_scope": [],
            "output": {"completed_steps": task["current_step"]},
            "artifacts": [],
            "metrics": {},
            "operation_ref": None,
            "log_ref": None,
            "failure": None,
            "next_action": "complete",
            "recorded_at": _now(),
        }
        return await self._verify_step(
            task,
            {"step_id": FINAL_STEP_ID, "verifier_refs": refs},
            result,
        )

    async def run(  # noqa: C901
        self, task_id: str, identity: dict[str, str], worker_id: str | None = None
    ) -> dict[str, Any]:
        worker_id = worker_id or str(uuid.uuid4())
        leased = self._acquire_lease(task_id, identity, worker_id)
        if leased is None:
            return self.get_task(task_id, identity)
        heartbeat = asyncio.create_task(self._heartbeats(task_id, identity, worker_id))
        try:
            while True:
                task = self.get_task(task_id, identity)
                if task.get("lease_owner") != worker_id:
                    raise RuntimeError("Task lease lost")
                if controlled := self._safe_control(task, identity):
                    return controlled
                if self._deadline_exceeded(task):
                    return self._fail(task_id, identity, "deadline_exceeded", task["version"])
                if task["current_step"] >= len(task["plan"]):
                    strict = task["task_spec"].get("execution_mode") == "strict"
                    if strict:
                        outcome = await self._verify_final(task)
                        if outcome.status == "blocked":
                            return self._transition(
                                task_id,
                                identity,
                                "verification_blocked",
                                "verification_blocked",
                                {"step_id": FINAL_STEP_ID, "error_code": outcome.error_code},
                                expected_version=task["version"],
                                finish_reason=outcome.error_code,
                            )
                        if outcome.status != "passed":
                            return self._transition(
                                task_id,
                                identity,
                                "verification_failed",
                                "verification_failed",
                                {"step_id": FINAL_STEP_ID, "error_code": outcome.error_code},
                                expected_version=task["version"],
                                finish_reason=outcome.error_code,
                            )
                        if self.evidence is not None:
                            self.evidence.request(task, identity, "succeeded")
                            pending = self._transition(
                                task_id,
                                identity,
                                "evidence_pending",
                                "evidence_requested",
                                {"run_id": task["run_id"]},
                                expected_version=task["version"],
                                finish_reason="evidence_pending",
                            )
                            try:
                                return self.reconcile_evidence(
                                    task_id, identity, pending["version"]
                                )
                            except (RuntimeError, ValueError):
                                return self.get_task(task_id, identity)
                    return self._transition(
                        task_id,
                        identity,
                        "succeeded",
                        "completed",
                        {"steps": task["current_step"], "run_id": task["run_id"]},
                        expected_version=task["version"],
                        finish_reason="verified_plan_completed" if strict else "plan_completed",
                    )
                if task["current_step"] >= task["max_steps"]:
                    return self._fail(task_id, identity, "max_steps_exceeded", task["version"])
                step = task["plan"][task["current_step"]]
                try:
                    spec = self.tools.get(step["tool"])
                    self.tools.validate(spec, step.get("arguments", {}), identity["role"])
                    self._validate_step_contract(task, step, spec, identity)
                    self._allow_call(spec, identity)
                except (PermissionError, RuntimeError, ValueError) as error:
                    return self._fail(task_id, identity, str(error), task["version"])
                if spec.requires_approval and not (task["approval"] or {}).get("approved"):
                    return self._transition(
                        task_id,
                        identity,
                        "waiting_approval",
                        "approval_requested",
                        {
                            "step": task["current_step"],
                            "step_id": step["step_id"],
                            "tool": spec.name,
                            "arguments": self._redact(
                                step.get("arguments", {}), spec.sensitive_fields
                            ),
                        },
                        expected_version=task["version"],
                        approval_json={
                            "step": task["current_step"],
                            "step_id": step["step_id"],
                            "tool": spec.name,
                            "arguments": step.get("arguments", {}),
                        },
                    )
                if spec.execution == "kubernetes_job":
                    if self.jobs is None:
                        return self._fail(
                            task_id, identity, "kubernetes_jobs_not_configured", task["version"]
                        )
                    try:
                        reserved, cached = self._reserve_tool_run(task, spec, step)
                        if cached is not None:
                            result = _decode(cached)
                        elif not reserved:
                            raise RuntimeError("Job tool is already running")
                        else:
                            self._start_attempt(task, spec, step)
                            job = self.jobs.request(task, {**step, "job_kind": spec.job_kind}, identity)
                    except (RuntimeError, ValueError) as error:
                        return self._fail(task_id, identity, str(error), task["version"])
                    if cached is None:
                        return self._transition(
                            task_id,
                            identity,
                            "waiting_job",
                            "job_requested",
                            {
                                "run_id": task["run_id"],
                                "step_id": step["step_id"],
                                "job_id": job["job_id"],
                            },
                            expected_version=task["version"],
                            finish_reason="waiting_job",
                        )
                else:
                    try:
                        arguments = (
                            {
                                **step.get("arguments", {}),
                                "_identity": identity,
                                "_h3_context": {
                                    "run_id": task["run_id"],
                                    "task_id": task["task_id"],
                                    "step_id": step["step_id"],
                                    "previous_artifacts": self._previous_artifacts(task, identity)
                                    if spec.name
                                    in {
                                        "refine_corpus",
                                        "publish_corpus",
                                        "resolve_conflict",
                                    }
                                    else [],
                                },
                            }
                            if spec.uses_identity
                            else step.get("arguments", {})
                        )
                        result = await self._call_tool(task, spec, arguments, step)
                    except ReconciliationRequired as error:
                        return self._transition(
                            task_id,
                            identity,
                            "reconciliation_required",
                            "reconciliation_required",
                            {"step_id": step["step_id"], "tool": spec.name, "reason": str(error)},
                            finish_reason=str(error),
                        )
                    except Exception as error:
                        return self._fail(task_id, identity, f"{spec.name}: {error}")
                if task["task_spec"].get("execution_mode") == "strict":
                    outcome = await self._verify_step(task, step, result)
                    if outcome.status == "blocked":
                        return self._transition(
                            task_id,
                            identity,
                            "verification_blocked",
                            "verification_blocked",
                            {"step_id": step["step_id"], "error_code": outcome.error_code},
                            expected_version=task["version"],
                            finish_reason=outcome.error_code,
                        )
                    if outcome.status != "passed":
                        return self._transition(
                            task_id,
                            identity,
                            "verification_failed",
                            "verification_failed",
                            {"step_id": step["step_id"], "error_code": outcome.error_code},
                            expected_version=task["version"],
                            finish_reason=outcome.error_code,
                        )
                with self.database.transaction(identity) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT * FROM agent_tasks WHERE task_id = %s FOR UPDATE", (task_id,)
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise PermissionError("Task not found")
                        current = self._row_to_task(row)
                        if (
                            current.get("lease_owner") != worker_id
                            or current["current_step"] != task["current_step"]
                        ):
                            raise RuntimeError("Task state changed while tool was running")
                        cursor.execute(
                            "UPDATE agent_tasks SET current_step = current_step + 1, approval_json = NULL, "
                            "updated_at = %s, version = version + 1 WHERE task_id = %s AND version = %s",
                            (_now(), task_id, current["version"]),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("Task state changed while recording tool result")
                        self._event(
                            cursor,
                            task_id,
                            identity["tenant_id"],
                            "observed",
                            {
                                "run_id": task["run_id"],
                                "step_id": step["step_id"],
                                "tool": spec.name,
                                "result_digest": _digest(result),
                            },
                        )
                self.audit.record(
                    identity,
                    "tool.call",
                    "tool",
                    resource_id=spec.name,
                    correlation_id=task["run_id"],
                    metadata={"result_digest": _digest(result)},
                )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            self._release_lease(task_id, identity, worker_id)

    async def _call_tool(
        self, task: dict[str, Any], spec: ToolSpec, arguments: dict[str, Any], step: dict[str, Any]
    ) -> Any:
        reserved, cached = self._reserve_tool_run(task, spec, step)
        if cached is not None:
            return _decode(cached)
        if not reserved:
            raise RuntimeError(f"Tool {spec.name!r} is already running")
        try:
            for attempt in range(spec.max_retries + 1):
                self._start_attempt(task, spec, step)
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(spec.handler, arguments), timeout=spec.timeout_seconds
                    )
                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(result, timeout=spec.timeout_seconds)
                    break
                except Exception:
                    if attempt == spec.max_retries:
                        raise
            else:  # pragma: no cover
                raise RuntimeError("unreachable tool retry loop")
        except Exception as error:
            if spec.side_effecting:
                self._finish_tool_run(task, spec, step, "reconciliation_required")
                raise ReconciliationRequired(
                    f"{spec.name}: result is uncertain after failure"
                ) from error
            failure = (
                self._failed_tool_result(spec, step, type(error).__name__)
                if task["task_spec"].get("execution_mode") == "strict"
                else None
            )
            self._finish_tool_run(task, spec, step, "failed", failure)
            raise
        try:
            result = self._tool_result(task, step, spec, result)
        except Exception as error:
            if spec.side_effecting:
                self._finish_tool_run(task, spec, step, "reconciliation_required")
                raise ReconciliationRequired(
                    f"{spec.name}: result contract failed after side effect"
                ) from error
            failure = (
                self._failed_tool_result(spec, step, type(error).__name__)
                if task["task_spec"].get("execution_mode") == "strict"
                else None
            )
            self._finish_tool_run(task, spec, step, "failed", failure)
            raise
        encoded = _json(result)
        if len(encoded.encode("utf-8")) > spec.max_result_bytes:
            failure = (
                self._failed_tool_result(spec, step, "result_too_large")
                if task["task_spec"].get("execution_mode") == "strict"
                else None
            )
            self._finish_tool_run(task, spec, step, "failed", failure)
            raise ValueError(f"Tool {spec.name!r} returned too much data")
        self._finish_tool_run(task, spec, step, "succeeded", encoded)
        return result

    def _reserve_tool_run(
        self, task: dict[str, Any], spec: ToolSpec, step: dict[str, Any]
    ) -> tuple[bool, Any]:
        identity = {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_tool_runs "
                    "(tenant_id, tool_name, idempotency_key, run_id, task_id, step_id, plan_version, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'reserved') ON CONFLICT DO NOTHING",
                    (
                        task["tenant_id"],
                        spec.name,
                        step["idempotency_key"],
                        task["run_id"],
                        task["task_id"],
                        step["step_id"],
                        step["created_in_plan_version"],
                    ),
                )
                inserted = cursor.rowcount == 1
                cursor.execute(
                    "SELECT result_json, state FROM agent_tool_runs WHERE tenant_id = %s AND tool_name = %s "
                    "AND idempotency_key = %s",
                    (task["tenant_id"], spec.name, step["idempotency_key"]),
                )
                row = cursor.fetchone()
                if row and row["result_json"] is not None:
                    return False, row["result_json"]
                if not inserted and row and row["state"] == "failed" and not spec.side_effecting:
                    cursor.execute(
                        "UPDATE agent_tool_runs SET state = 'reserved', completed_at = NULL WHERE tenant_id = %s "
                        "AND tool_name = %s AND idempotency_key = %s",
                        (task["tenant_id"], spec.name, step["idempotency_key"]),
                    )
                    return True, None
                if not inserted and row and row["state"] == "reconciliation_required":
                    raise ReconciliationRequired(f"{spec.name}: reconciliation is required")
                if not inserted and row and row["state"] == "running" and spec.side_effecting:
                    self._finish_tool_run(task, spec, step, "reconciliation_required")
                    raise ReconciliationRequired(
                        f"{spec.name}: previous attempt outcome is unknown"
                    )
        return inserted, None

    def _start_attempt(self, task: dict[str, Any], spec: ToolSpec, step: dict[str, Any]) -> None:
        identity = {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tool_runs SET state = 'running', attempt = attempt + 1, started_at = now() "
                    "WHERE tenant_id = %s AND tool_name = %s AND idempotency_key = %s",
                    (task["tenant_id"], spec.name, step["idempotency_key"]),
                )
                self._event(
                    cursor,
                    task["task_id"],
                    task["tenant_id"],
                    "tool_attempt_started",
                    {"run_id": task["run_id"], "step_id": step["step_id"], "tool": spec.name},
                )

    def _finish_tool_run(
        self,
        task: dict[str, Any],
        spec: ToolSpec,
        step: dict[str, Any],
        state: str,
        result: str | None = None,
    ) -> None:
        identity = {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_tool_runs SET state = %s, result_json = COALESCE(result_json, %s::jsonb), "
                    "completed_at = now() WHERE tenant_id = %s AND tool_name = %s AND idempotency_key = %s",
                    (state, result, task["tenant_id"], spec.name, step["idempotency_key"]),
                )
                if state != "succeeded":
                    self._event(
                        cursor,
                        task["task_id"],
                        task["tenant_id"],
                        "tool_attempt_failed",
                        {
                            "run_id": task["run_id"],
                            "step_id": step["step_id"],
                            "tool": spec.name,
                            "state": state,
                        },
                    )

    def _fail(
        self,
        task_id: str,
        identity: dict[str, str],
        reason: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        task = self._transition(
            task_id,
            identity,
            "failed",
            "failed",
            {"reason": reason},
            expected_version=expected_version,
            finish_reason=reason,
        )
        if self.evidence is not None:
            try:
                self.evidence.request(task, identity, "failed")
                self.evidence.reconcile(task, identity)
            except Exception:  # evidence must not hide the business failure
                pass
        self.audit.record(
            identity,
            "task.failed",
            "task",
            outcome="failed",
            resource_id=task_id,
            correlation_id=task["run_id"],
            metadata={"reason": reason},
        )
        return task
