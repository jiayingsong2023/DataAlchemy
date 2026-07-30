"""Adapters that expose the existing Coordinator capabilities as Phase 1 tools."""

import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any

from config import (
    DATABASE_URL,
    GIT_PILOT_READERS,
    GIT_PILOT_REPOSITORY,
    GIT_PILOT_TOKEN,
    PILOT_RUNS_DIR,
    S3_BUCKET,
)
from connectors.git import GitConnector
from connectors.git_ingestion import prepare_git_document
from storage.audit import AuditLog
from utils.s3_utils import S3Utils

from .agent_runtime import ToolRegistry, ToolSpec


def _ingest_document(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Publish one already-landed Markdown/TXT object after an approved task."""
    identity = arguments.pop("_identity")
    object_key = arguments["object_key"].strip()
    if not object_key.startswith("raw/documents/") or object_key == "raw/documents/":
        raise ValueError("object_key must be under raw/documents/")
    raw = S3Utils().get_object_body(object_key)
    if raw is None:
        raise RuntimeError("raw document was not found in object storage")
    filename = object_key.rsplit("/", 1)[-1]
    document, chunker, rejection = prepare_git_document(
        filename,
        raw,
        f"s3://{S3_BUCKET}/{object_key}",
        {"raw_object_key": object_key, "source_type": "pilot_document"},
    )
    if rejection:
        raise ValueError(f"document rejected: {rejection}")
    assert document is not None and chunker is not None
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    document_ids = coordinator.agent_manager.agent_c.vs.add_documents([document], identity, chunker)
    AuditLog(DATABASE_URL).record(
        identity,
        "document.ingest",
        "document",
        resource_id=document_ids[0],
        metadata={"object_key": object_key},
    )
    return {"status": "completed", "document_id": document_ids[0], "object_key": object_key}


def _sync_git(coordinator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    identity = arguments.pop("_identity")
    if not GIT_PILOT_REPOSITORY:
        raise RuntimeError("GIT_PILOT_REPOSITORY is required")
    coordinator.agent_manager.lazy_load_agents(need_c=True)
    readers = [("user", name.strip()) for name in GIT_PILOT_READERS.split(",") if name.strip()]
    return GitConnector(DATABASE_URL, GIT_PILOT_REPOSITORY, GIT_PILOT_TOKEN).sync(
        identity,
        vector_store=coordinator.agent_manager.agent_c.vs,
        acl=readers,
        runs_dir=PILOT_RUNS_DIR,
    )


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
            name="ingest_document",
            handler=partial(_ingest_document, coordinator),
            schema={
                "type": "object",
                "required": ["object_key"],
                "properties": {"object_key": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            uses_identity=True,
            timeout_seconds=300,
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
            handler=partial(_sync_git, coordinator),
            schema={"type": "object", "additionalProperties": False},
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            uses_identity=True,
            timeout_seconds=60,
            max_calls_per_minute=6,
            max_retries=1,
            sensitive_fields=frozenset({"token", "authorization"}),
        )
    )
