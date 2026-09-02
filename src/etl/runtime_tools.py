"""Governed document, corpus, and blocked legacy tools."""

from functools import partial
from typing import Any

from core.runtime_tool_handlers import (
    _compare_sources,
    _document_result,
    _document_scope,
    _h3_artifact_scope,
    _h3_input_scope,
    _h3_publish_scope,
    _ingest_document,
    _publish_corpus,
    _rag_probe,
    _refine_corpus,
    _resolve_conflict,
    _rough_clean_scope,
    _validate_document_input,
)
from core.tool_contracts import ToolRegistry, ToolSpec


def register_etl_tools(registry: ToolRegistry, *, vector_store: Any, chat_retriever: Any) -> None:
    def blocked_legacy_tool(_: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError("legacy inline tool is blocked")

    registry.register(
        ToolSpec(
            name="spark_rough_clean",
            handler=lambda _arguments: (_ for _ in ()).throw(
                RuntimeError("Spark jobs are not inline tools")
            ),
            schema={
                "type": "object",
                "required": ["input_key", "input_sha256"],
                "properties": {
                    "input_key": {"type": "string"},
                    "input_sha256": {"type": "string"},
                    "deadline_seconds": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            version=1,
            execution="kubernetes_job",
            job_kind="spark_rough_clean",
            scope_resolver=_rough_clean_scope,
            expected_artifacts=frozenset({("minio", "cleaned_corpus")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="validate_document_input",
            handler=_validate_document_input,
            schema={
                "type": "object",
                "required": ["input_key", "input_sha256"],
                "properties": {
                    "input_key": {"type": "string"},
                    "input_sha256": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=_h3_input_scope,
            expected_artifacts=frozenset({("minio", "input_manifest")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="refine_corpus",
            handler=_refine_corpus,
            schema={
                "type": "object",
                "required": ["input_key"],
                "properties": {
                    "input_key": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=_h3_artifact_scope,
            expected_artifacts=frozenset(
                {
                    ("minio", "canonical_content"),
                    ("minio", "rag_projection"),
                    ("minio", "quarantine"),
                }
            ),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="publish_corpus",
            handler=partial(_publish_corpus, vector_store),
            schema={
                "type": "object",
                "required": ["input_key"],
                "properties": {"input_key": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=_h3_publish_scope,
            expected_artifacts=frozenset({("postgres", "document")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="rag_probe",
            handler=partial(_rag_probe, chat_retriever),
            schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"user", "admin"}),
            uses_identity=True,
            timeout_seconds=300,
            version=1,
            scope_resolver=lambda _arguments, identity: [
                f"postgres:tenant:{identity['tenant_id']}"
            ],
            expected_artifacts=frozenset({("minio", "retrieval_report")}),
            result_sensitivity={"citations": "public", "*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="compare_sources",
            handler=_compare_sources,
            schema={
                "type": "object",
                "required": ["claim_key", "candidates"],
                "properties": {
                    "claim_key": {"type": "string"},
                    "candidates": {"type": "array"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=lambda _arguments, identity: [
                f"postgres:tenant:{identity['tenant_id']}"
            ],
            expected_artifacts=frozenset({("minio", "conflict_report")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="resolve_conflict",
            handler=_resolve_conflict,
            schema={
                "type": "object",
                "required": ["report_key", "candidate_id"],
                "properties": {
                    "report_key": {"type": "string"},
                    "candidate_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=60,
            version=1,
            scope_resolver=lambda arguments, _identity: [f"artifact:{arguments['report_key']}"],
            expected_artifacts=frozenset({("minio", "conflict_decision")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="ingest_document",
            handler=partial(_ingest_document, vector_store),
            schema={
                "type": "object",
                "required": ["object_key"],
                "properties": {"object_key": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            timeout_seconds=300,
            version=2,
            scope_resolver=_document_scope,
            result_validator=_document_result,
            expected_artifacts=frozenset({("postgres", "document")}),
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="ingest",
            handler=blocked_legacy_tool,
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
            side_effecting=True,
            blocked_reason="requires H2 job evidence",
        )
    )
    for name in ("train", "evaluate", "release"):
        registry.register(
            ToolSpec(
                name=name,
                handler=blocked_legacy_tool,
                schema={"type": "object", "additionalProperties": False},
                roles=frozenset({"admin"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=name in {"train", "release"},
                blocked_reason="requires H2/H5 evidence and release gates",
            )
        )
