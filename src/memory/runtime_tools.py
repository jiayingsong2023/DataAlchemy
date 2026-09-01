"""Governed context and memory tools."""

from typing import Any

from config import DATABASE_URL
from core.runtime_tool_handlers import _context_policy_scope, _context_session_scope
from core.tool_contracts import ToolRegistry, ToolSpec
from memory.context import ContextService


def register_memory_tools(registry: ToolRegistry, memory: Any) -> None:
    def compact_context(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        service = ContextService(DATABASE_URL)
        result = service.compact(
            arguments["session_id"], identity, summary=arguments.get("summary")
        )
        return {**result, "observed_scope": [f"session:{arguments['session_id']}"]}

    def distill_memory_candidates(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        service = ContextService(DATABASE_URL)
        session_id = arguments["session_id"]
        events = service.events(session_id, identity)
        candidates = service.extract_candidates(events)
        session = service.get_session(session_id, identity)
        decisions = []
        for item in candidates:
            decision = memory.create_governed_candidate(
                identity, item, auto_memory_enabled=session["auto_memory_enabled"]
            )
            decisions.append({**item, **decision})
        return {
            "candidates": decisions,
            "session_id": session_id,
            "observed_scope": [f"session:{session_id}"],
        }

    def apply_memory_policy(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        decisions = []
        for memory_id in arguments.get("memory_ids", []):
            rows = memory.list(identity)
            row = next((item for item in rows if item["memory_id"] == memory_id), None)
            if row is None:
                raise PermissionError("memory candidate is outside the tenant scope")
            decisions.append(
                {"memory_id": memory_id, "status": row["status"], "reason": row["decision_reason"]}
            )
        return {"decisions": decisions, "observed_scope": [f"tenant:{identity['tenant_id']}"]}

    for name, handler, required in (
        ("compact_context", compact_context, ["session_id"]),
        ("distill_memory_candidates", distill_memory_candidates, ["session_id"]),
        ("apply_memory_policy", apply_memory_policy, []),
    ):
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema={
                    "type": "object",
                    "required": required,
                    "properties": {
                        "session_id": {"type": "string"},
                        "memory_ids": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                idempotent=True,
                side_effecting=name != "apply_memory_policy",
                uses_identity=True,
                scope_resolver=_context_policy_scope
                if name == "apply_memory_policy"
                else _context_session_scope,
                result_sensitivity={"*": "internal"},
            )
        )
