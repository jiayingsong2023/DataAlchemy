"""The single, durable task runtime used by Phase 1."""

import asyncio
import inspect
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[[dict[str, Any]], Any | Awaitable[Any]]
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    roles: frozenset[str] = frozenset({"user", "admin"})
    timeout_seconds: float = 60.0
    requires_approval: bool = False
    idempotent: bool = False
    max_result_bytes: int = 65_536


class ToolRegistry:
    """Small tool gateway: one registry, validation and authorization point."""

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
        self._validate_schema(schema, arguments)

    @staticmethod
    def _validate_schema(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        required = set(schema.get("required", []))
        missing = required - arguments.keys()
        if missing:
            raise ValueError(f"Missing tool arguments: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = arguments.keys() - properties.keys()
            if unexpected:
                raise ValueError(f"Unexpected tool arguments: {', '.join(sorted(unexpected))}")
        for name, value in arguments.items():
            expected = properties.get(name, {}).get("type")
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"Tool argument {name!r} must be a string")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"Tool argument {name!r} must be an integer")
            if expected == "boolean" and not isinstance(value, bool):
                raise ValueError(f"Tool argument {name!r} must be a boolean")


class AgentRuntime:
    """Plan → Act → Observe → Replan with SQLite checkpoints and append-only events."""

    terminal_states = frozenset({"succeeded", "failed", "cancelled"})

    def __init__(self, db_path: str, tools: ToolRegistry):
        self.db_path = db_path
        self.tools = tools
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    role TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    max_steps INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    budget_json TEXT NOT NULL,
                    approval_json TEXT,
                    finish_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES agent_tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS agent_tool_runs (
                    tenant_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, tool_name, idempotency_key)
                );
                """
            )

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
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_tasks
                   (task_id, tenant_id, owner, role, goal, state, plan_json, max_steps,
                    budget_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?)""",
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
            self._event(connection, task_id, "planned", {"goal": goal, "plan": plan})
        return self.get_task(task_id, identity)

    def _event(
        self, connection: sqlite3.Connection, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO agent_events "
            "(task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event_type, _json(payload), _now()),
        )

    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        for key in ("plan_json", "budget_json", "approval_json"):
            task[key.removesuffix("_json")] = json.loads(task.pop(key) or "null")
        return task

    def _assert_access(self, task: dict[str, Any], identity: dict[str, str]) -> None:
        if task["tenant_id"] != identity["tenant_id"]:
            raise PermissionError("Task not found")
        if task["owner"] != identity["username"] and identity["role"] != "admin":
            raise PermissionError("Task not found")

    def get_task(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Task not found")
        task = self._row_to_task(row)
        self._assert_access(task, identity)
        return task

    def list_tasks(self, identity: dict[str, str]) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_tasks WHERE tenant_id = ?"
        values: list[str] = [identity["tenant_id"]]
        if identity["role"] != "admin":
            query += " AND owner = ?"
            values.append(identity["username"])
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._row_to_task(row) for row in rows]

    def events(self, task_id: str, identity: dict[str, str]) -> list[dict[str, Any]]:
        self.get_task(task_id, identity)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, event_type, payload_json, created_at FROM agent_events "
                "WHERE task_id = ? ORDER BY event_id",
                (task_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def pause(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states:
            raise ValueError("Terminal tasks cannot be paused")
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = 'paused', updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._event(connection, task_id, "paused", {"by": identity["username"]})
        return self.get_task(task_id, identity)

    def resume(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "paused":
            raise ValueError("Only paused tasks can be resumed")
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = 'created', updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._event(connection, task_id, "resumed", {"by": identity["username"]})
        return self.get_task(task_id, identity)

    def retry(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "failed":
            raise ValueError("Only failed tasks can be retried")
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = 'created', finish_reason = NULL, updated_at = ? "
                "WHERE task_id = ?",
                (_now(), task_id),
            )
            self._event(connection, task_id, "retried", {"by": identity["username"]})
        return self.get_task(task_id, identity)

    def approve(self, task_id: str, identity: dict[str, str], approved: bool) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] != "waiting_approval" or not task["approval"]:
            raise ValueError("Task is not waiting for approval")
        state = "running" if approved else "cancelled"
        reason = None if approved else "approval_rejected"
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = ?, approval_json = ?, finish_reason = ?, "
                "updated_at = ? WHERE task_id = ?",
                (
                    state,
                    _json(
                        {
                            **task["approval"],
                            "approved": approved,
                            "approved_by": identity["username"],
                        }
                    ),
                    reason,
                    _now(),
                    task_id,
                ),
            )
            self._event(
                connection,
                task_id,
                "approval_granted" if approved else "approval_rejected",
                {"by": identity["username"]},
            )
        return self.get_task(task_id, identity)

    async def run(self, task_id: str, identity: dict[str, str]) -> dict[str, Any]:
        task = self.get_task(task_id, identity)
        if task["state"] in self.terminal_states | {"paused", "waiting_approval"}:
            return task
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = 'running', updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._event(connection, task_id, "started", {"by": identity["username"]})

        while True:
            task = self.get_task(task_id, identity)
            if task["current_step"] >= len(task["plan"]):
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE agent_tasks SET state = 'succeeded', "
                        "finish_reason = 'plan_completed', updated_at = ? WHERE task_id = ?",
                        (_now(), task_id),
                    )
                    self._event(connection, task_id, "completed", {"steps": task["current_step"]})
                return self.get_task(task_id, identity)
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
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE agent_tasks SET state = 'waiting_approval', approval_json = ?, "
                        "updated_at = ? WHERE task_id = ?",
                        (_json(approval), _now(), task_id),
                    )
                    self._event(connection, task_id, "approval_requested", approval)
                return self.get_task(task_id, identity)

            try:
                result = await self._call_tool(task, spec, arguments, step.get("idempotency_key"))
            except Exception as error:
                return self._fail(task_id, identity, f"{spec.name}: {error}")

            with self._connect() as connection:
                connection.execute(
                    "UPDATE agent_tasks SET current_step = current_step + 1, approval_json = NULL, "
                    "updated_at = ? WHERE task_id = ?",
                    (_now(), task_id),
                )
                self._event(connection, task_id, "observed", {"tool": spec.name, "result": result})
                self._event(
                    connection, task_id, "replanned", {"next_step": task["current_step"] + 1}
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
        if cached:
            return json.loads(cached)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(spec.handler, arguments), timeout=spec.timeout_seconds
            )
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=spec.timeout_seconds)
        except Exception:
            if reserved:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM agent_tool_runs WHERE tenant_id = ? AND tool_name = ? "
                        "AND idempotency_key = ? AND result_json IS NULL",
                        (task["tenant_id"], spec.name, idempotency_key),
                    )
            raise
        encoded = _json(result)
        if len(encoded.encode("utf-8")) > spec.max_result_bytes:
            raise ValueError(f"Tool {spec.name!r} returned too much data")
        if idempotency_key:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE agent_tool_runs SET result_json = ? WHERE tenant_id = ? "
                    "AND tool_name = ? AND idempotency_key = ?",
                    (encoded, task["tenant_id"], spec.name, idempotency_key),
                )
        return result

    def _reserve_idempotency(
        self, task: dict[str, Any], spec: ToolSpec, idempotency_key: str | None
    ) -> tuple[bool, str | None]:
        if not idempotency_key:
            return False, None
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO agent_tool_runs "
                "(tenant_id, tool_name, idempotency_key, result_json, created_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (task["tenant_id"], spec.name, idempotency_key, _now()),
            ).rowcount
            row = connection.execute(
                "SELECT result_json FROM agent_tool_runs WHERE tenant_id = ? "
                "AND tool_name = ? AND idempotency_key = ?",
                (task["tenant_id"], spec.name, idempotency_key),
            ).fetchone()
        if row and row["result_json"]:
            return False, row["result_json"]
        if not inserted:
            raise RuntimeError(f"Tool {spec.name!r} is already running for this idempotency key")
        return True, None

    def _fail(self, task_id: str, identity: dict[str, str], reason: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE agent_tasks SET state = 'failed', finish_reason = ?, "
                "updated_at = ? WHERE task_id = ?",
                (reason, _now(), task_id),
            )
            self._event(connection, task_id, "failed", {"reason": reason})
        return self.get_task(task_id, identity)
