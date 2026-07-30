"""Adapters that expose the existing Coordinator capabilities as Phase 1 tools."""

import subprocess
import sys
from pathlib import Path
from typing import Any

from config import DATABASE_URL, GIT_PILOT_REPOSITORY, GIT_PILOT_TOKEN, PILOT_RUNS_DIR
from connectors.git import GitConnector

from .agent_runtime import ToolRegistry, ToolSpec


def register_coordinator_tools(registry: ToolRegistry, coordinator: Any) -> None:
    async def chat(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        return {"answer": await coordinator.chat_async(arguments["query"], identity)}

    def ingest(arguments: dict[str, Any]) -> dict[str, str]:
        coordinator.run_ingestion_pipeline(
            stage=arguments.get("stage", "all"),
            synthesis=arguments.get("synthesis", False),
            max_samples=arguments.get("max_samples"),
        )
        return {"status": "completed"}

    def train(_: dict[str, Any]) -> dict[str, str]:
        coordinator.run_training_pipeline()
        return {"status": "completed"}

    def release(_: dict[str, Any]) -> dict[str, str]:
        if not coordinator.reload_model():
            raise RuntimeError("Model reload failed")
        return {"status": "completed"}

    def evaluate(_: dict[str, Any]) -> dict[str, str]:
        script = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_phase1_baseline.py"
        completed = subprocess.run(
            [sys.executable, str(script)], check=False, capture_output=True, text=True
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "Phase 1 evaluation failed")
        return {"status": "completed", "summary": completed.stdout.strip()}

    def sync_git(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        if not GIT_PILOT_REPOSITORY:
            raise RuntimeError("GIT_PILOT_REPOSITORY is required")
        return GitConnector(DATABASE_URL, GIT_PILOT_REPOSITORY, GIT_PILOT_TOKEN).sync(
            identity, runs_dir=PILOT_RUNS_DIR
        )

    registry.register(
        ToolSpec(
            name="rag_chat",
            handler=chat,
            schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            timeout_seconds=300,
            uses_identity=True,
        )
    )
    registry.register(
        ToolSpec(
            name="ingest",
            handler=ingest,
            schema={
                "type": "object",
                "properties": {
                    "stage": {"type": "string"},
                    "synthesis": {"type": "boolean"},
                    "max_samples": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
        )
    )
    for name, handler in {"train": train, "evaluate": evaluate, "release": release}.items():
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema={"type": "object", "additionalProperties": False},
                roles=frozenset({"admin"}),
                requires_approval=True,
                idempotent=True,
            )
        )
    registry.register(
        ToolSpec(
            name="sync_git",
            handler=sync_git,
            schema={"type": "object", "additionalProperties": False},
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            uses_identity=True,
            timeout_seconds=60,
        )
    )
