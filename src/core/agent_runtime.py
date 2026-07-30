"""The single, durable PostgreSQL task runtime used by Phase 2."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from storage.postgres import PostgresDatabase


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[dict[str, Any]], Any | Awaitable[Any]]
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    roles: frozenset[str] = frozenset({"user", "admin"})
    timeout_seconds: float = 60.0
    requires_approval: bool = False
    idempotent: bool = False
    uses_identity: bool = False
    max_result_bytes: int = 65_536


class ToolRegistry:
    """One validation and authorization point for registered tools."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ValueError(f"Unknown tool: {name}") from error

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
    """Plan → Act → Observe → Replan using PostgreSQL events and checkpoints."""

    terminal_states = frozenset({"succeeded", "failed", "cancelled"})

    def __init__(self, database_url: str, tools: ToolRegistry):
        self.database = PostgresDatabase(database_url)
        self.tools = tools

    def create_task(
        self,
        identity: dict[str, str],
        goal: str,
        plan: list[dict[str, Any]],
        max_steps: int = 8,
        budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("Task goal cannot be empty")
        if not plan or len(plan) > max_steps or max_steps < 1:
            raise ValueError("Task plan must contain no more than max_steps steps")
        for step in plan:
            self.tools.get(step.get("tool", ""))
            if not isinstance(step.get("arguments", {}), dict):
                raise ValueError("Each task step needs object arguments")
        task_id = str(uuid.uuid4())
        now = _now()
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_tasks "
                    "(task_id, tenant_id, owner, role, goal, state, plan_json, max_steps, "
                    "budget_json, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'created', %s::jsonb, %s, %s::jsonb, %s, %s)",
                    (
                        task_id,
                        identity["tenant_id"],
                        identity["username"],
                        identity["role"],
                        goal,
                        _json(plan),
                        max_steps,
                        _json(budget or {}),
                        now,
                        now,
                    ),
                )
                self._event(
                    cursor, task_id, identity["tenant_id"], "planned", {"goal": goal, "plan": plan}
                )
        return self.get_task(task_id, identity)

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
        for key in ("plan_json", "budget_json", "approval_json"):
            task[key.removesuffix("_json")] = _decode(task.pop(key))
        task["task_id"] = str(task["task_id"])
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

    def _transition(
        self,
        task_id: str,
        identity: dict[str, str],
        state: str,
        event: str,
        payload: dict[str, Any],
        **updates: Any,
    ) -> dict[str, Any]:
        assignments = ["state = %s", "updated_at = %s", "version = version + 1"]
        values: list[Any] = [state, _now()]
        for column, value in updates.items():
            assignments.append(f"{column} = %s")
            values.append(_json(value) if column.endswith("_json") else value)
        values.append(task_id)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE task_id = %s", values
                )
                if cursor.rowcount != 1:
                    raise PermissionError("Task not found")
                self._event(cursor, task_id, identity["tenant_id"], event, payload)
        return self.get_task(task_id, identity)

    def pause(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states:
            raise ValueError("Terminal tasks cannot be paused")
        return self._transition(task_id, identity, "paused", "paused", {"by": identity["username"]})

    def resume(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        if self.get_task(task_id, identity)["state"] != "paused":
            raise ValueError("Only paused tasks can be resumed")
        return self._transition(
            task_id, identity, "created", "resumed", {"by": identity["username"]}
        )

    def retry(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        if self.get_task(task_id, identity)["state"] != "failed":
            raise ValueError("Only failed tasks can be retried")
        return self._transition(
            task_id,
            identity,
            "created",
            "retried",
            {"by": identity["username"]},
            finish_reason=None,
        )

    def approve(self, task_id: str, identity: dict[str, str], approved: bool) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "waiting_approval" or not task["approval"]:
            raise ValueError("Task is not waiting for approval")
        approval = {**task["approval"], "approved": approved, "approved_by": identity["username"]}
        return self._transition(
            task_id,
            identity,
            "running" if approved else "cancelled",
            "approval_granted" if approved else "approval_rejected",
            {"by": identity["username"]},
            approval_json=approval,
            finish_reason=None if approved else "approval_rejected",
        )

    async def run(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states | {"paused", "waiting_approval"}:
            return task
        self._transition(task_id, identity, "running", "started", {"by": identity["username"]})
        while True:
            task = self.get_task(task_id, identity)
            if task["current_step"] >= len(task["plan"]):
                return self._transition(
                    task_id,
                    identity,
                    "succeeded",
                    "completed",
                    {"steps": task["current_step"]},
                    finish_reason="plan_completed",
                )
            if task["current_step"] >= task["max_steps"]:
                return self._fail(task_id, identity, "max_steps_exceeded")
            step = task["plan"][task["current_step"]]
            try:
                spec = self.tools.get(step["tool"])
                arguments = step.get("arguments", {})
                self.tools.validate(spec, arguments, identity["role"])
            except (KeyError, PermissionError, ValueError) as error:
                return self._fail(task_id, identity, str(error))
            if spec.requires_approval and not (task["approval"] or {}).get("approved"):
                approval = {"step": task["current_step"], "tool": spec.name, "arguments": arguments}
                return self._transition(
                    task_id,
                    identity,
                    "waiting_approval",
                    "approval_requested",
                    approval,
                    approval_json=approval,
                )
            try:
                tool_arguments = (
                    {**arguments, "_identity": identity} if spec.uses_identity else arguments
                )
                result = await self._call_tool(
                    task, spec, tool_arguments, step.get("idempotency_key")
                )
            except Exception as error:
                return self._fail(task_id, identity, f"{spec.name}: {error}")
            with self.database.transaction(identity) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_tasks SET current_step = current_step + 1, "
                        "approval_json = NULL, updated_at = %s, version = version + 1 "
                        "WHERE task_id = %s",
                        (_now(), task_id),
                    )
                    self._event(
                        cursor,
                        task_id,
                        identity["tenant_id"],
                        "observed",
                        {"tool": spec.name, "result": result},
                    )
                    self._event(
                        cursor,
                        task_id,
                        identity["tenant_id"],
                        "replanned",
                        {"next_step": task["current_step"] + 1},
                    )

    async def _call_tool(
        self,
        task: dict[str, Any],
        spec: ToolSpec,
        arguments: dict[str, Any],
        idempotency_key: str | None,
    ) -> Any:
        if spec.idempotent and not idempotency_key:
            raise ValueError(f"Tool {spec.name!r} requires an idempotency key")
        reserved, cached = self._reserve_idempotency(task, spec, idempotency_key)
        if cached is not None:
            return _decode(cached)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(spec.handler, arguments), timeout=spec.timeout_seconds
            )
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=spec.timeout_seconds)
        except Exception:
            if reserved:
                with self.database.transaction(
                    {
                        "tenant_id": task["tenant_id"],
                        "username": task["owner"],
                        "role": task["role"],
                    }
                ) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM agent_tool_runs WHERE tenant_id = %s AND tool_name = %s "
                            "AND idempotency_key = %s AND result_json IS NULL",
                            (task["tenant_id"], spec.name, idempotency_key),
                        )
            raise
        encoded = _json(result)
        if len(encoded.encode("utf-8")) > spec.max_result_bytes:
            raise ValueError(f"Tool {spec.name!r} returned too much data")
        if idempotency_key:
            with self.database.transaction(
                {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_tool_runs SET result_json = %s::jsonb WHERE tenant_id = %s "
                        "AND tool_name = %s AND idempotency_key = %s",
                        (encoded, task["tenant_id"], spec.name, idempotency_key),
                    )
        return result

    def _reserve_idempotency(
        self, task: dict[str, Any], spec: ToolSpec, key: str | None
    ) -> tuple[bool, Any]:
        if not key:
            return False, None
        identity = {"tenant_id": task["tenant_id"], "username": task["owner"], "role": task["role"]}
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_tool_runs "
                    "(tenant_id, tool_name, idempotency_key) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (task["tenant_id"], spec.name, key),
                )
                inserted = cursor.rowcount == 1
                cursor.execute(
                    "SELECT result_json FROM agent_tool_runs WHERE tenant_id = %s "
                    "AND tool_name = %s "
                    "AND idempotency_key = %s",
                    (task["tenant_id"], spec.name, key),
                )
                row = cursor.fetchone()
        if row and row["result_json"] is not None:
            return False, row["result_json"]
        if not inserted:
            raise RuntimeError(f"Tool {spec.name!r} is already running for this idempotency key")
        return True, None

    def _fail(self, task_id: str, identity: dict[str, str], reason: str) -> dict[str, Any]:
        return self._transition(
            task_id, identity, "failed", "failed", {"reason": reason}, finish_reason=reason
        )
