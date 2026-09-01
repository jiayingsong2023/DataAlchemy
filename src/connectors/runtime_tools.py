"""Governed connector tools."""

from functools import partial
from typing import Any

from core.runtime_tool_handlers import _git_scope, _sync_git
from core.tool_contracts import ToolRegistry, ToolSpec


def register_connector_tools(registry: ToolRegistry, *, vector_store: Any) -> None:
    registry.register(
        ToolSpec(
            name="sync_git",
            handler=partial(_sync_git, vector_store),
            schema={"type": "object", "additionalProperties": False},
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=60,
            max_calls_per_minute=6,
            max_retries=1,
            sensitive_fields=frozenset({"token", "authorization"}),
            version=2,
            scope_resolver=_git_scope,
            result_sensitivity={"*": "internal"},
        )
    )
