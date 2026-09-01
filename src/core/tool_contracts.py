"""Governed tool contracts shared by the runtime and tool registrars."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


def _digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
