"""Governed evaluation, adapter, and release tools."""

from typing import Any

from config import DATABASE_URL
from core.runtime_tool_handlers import _h5_scope
from core.tool_contracts import ToolRegistry, ToolSpec
from harness.evaluation import EvaluationService
from release.governance import ReleaseGovernance


def register_release_tools(  # noqa: C901 - registration mirrors independent governance actions
    registry: ToolRegistry,
) -> None:
    def h5_create_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        snapshot_id = service.create_snapshot(identity, **arguments)
        return {"snapshot_id": snapshot_id, "status": "candidate"}

    def h5_create_evaluation(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        evaluation_id = service.create_campaign(
            identity,
            arguments["suite"],
            subject_type=arguments["subject_type"],
            subject_ref=arguments["subject_ref"],
            required_trials=arguments.get("required_trials", 3),
        )
        return {"evaluation_id": evaluation_id, "status": "draft"}

    def h5_approve_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.approve_snapshot(identity, arguments["snapshot_id"])
        return {"snapshot_id": arguments["snapshot_id"], "status": "approved"}

    def h5_revoke_snapshot(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.revoke_snapshot(identity, arguments["snapshot_id"], arguments["reason"])
        return {"snapshot_id": arguments["snapshot_id"], "status": "revoked"}

    def h5_create_adapter(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        adapter_id = service.create_adapter_candidate(identity, **arguments)
        return {"adapter_id": adapter_id, "status": "candidate"}

    def h5_verify_adapter(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        service = EvaluationService(DATABASE_URL)
        service.verify_adapter(identity, arguments["adapter_id"], arguments["evaluation_id"])
        return {"adapter_id": arguments["adapter_id"], "status": "verified"}

    def h5_create_release_candidate(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        release_id = ReleaseGovernance(DATABASE_URL).create_candidate(
            identity, arguments["manifest"]
        )
        return {"release_id": release_id, "status": "candidate"}

    def h5_advance_release(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        return ReleaseGovernance(DATABASE_URL).advance(
            arguments["release_id"],
            arguments["target"],
            identity,
            arguments.get("expected_version"),
        )

    def h5_observe_release(arguments: dict[str, Any]) -> dict[str, str]:
        identity = arguments.pop("_identity")
        status = ReleaseGovernance(DATABASE_URL).observe(
            arguments["release_id"], arguments["metrics"], identity
        )
        return {"release_id": arguments["release_id"], "status": status}

    registry.register(
        ToolSpec(
            name="h5_create_evaluation",
            handler=h5_create_evaluation,
            schema={
                "type": "object",
                "required": ["suite", "subject_type", "subject_ref"],
                "properties": {
                    "suite": {"type": "object"},
                    "subject_type": {"type": "string"},
                    "subject_ref": {"type": "string"},
                    "required_trials": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_create_snapshot",
            handler=h5_create_snapshot,
            schema={
                "type": "object",
                "required": [
                    "annotation_items",
                    "dataset_key",
                    "dataset_sha256",
                    "dataset_size",
                    "base_model_digest",
                    "policy_version",
                    "compile_manifest_key",
                    "compile_manifest_sha256",
                    "target_tokenizer_digest",
                    "chat_template_digest",
                ],
                "properties": {
                    "annotation_items": {"type": "array"},
                    "dataset_key": {"type": "string"},
                    "dataset_sha256": {"type": "string"},
                    "dataset_size": {"type": "integer"},
                    "base_model_digest": {"type": "string"},
                    "policy_version": {"type": "string"},
                    "compile_manifest_key": {"type": "string"},
                    "compile_manifest_sha256": {"type": "string"},
                    "target_tokenizer_digest": {"type": "string"},
                    "chat_template_digest": {"type": "string"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    for name, handler, schema in (
        (
            "h5_approve_snapshot",
            h5_approve_snapshot,
            {
                "type": "object",
                "required": ["snapshot_id"],
                "properties": {"snapshot_id": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        (
            "h5_revoke_snapshot",
            h5_revoke_snapshot,
            {
                "type": "object",
                "required": ["snapshot_id", "reason"],
                "properties": {"snapshot_id": {"type": "string"}, "reason": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        (
            "h5_verify_adapter",
            h5_verify_adapter,
            {
                "type": "object",
                "required": ["adapter_id", "evaluation_id"],
                "properties": {
                    "adapter_id": {"type": "string"},
                    "evaluation_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
    ):
        registry.register(
            ToolSpec(
                name=name,
                handler=handler,
                schema=schema,
                roles=frozenset({"admin", "reviewer"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=True,
                uses_identity=True,
                scope_resolver=_h5_scope,
                result_sensitivity={"*": "internal"},
            )
        )
    registry.register(
        ToolSpec(
            name="h5_create_adapter",
            handler=h5_create_adapter,
            schema={
                "type": "object",
                "required": [
                    "snapshot_id",
                    "base_model_digest",
                    "tokenizer_digest",
                    "artifact_key",
                    "artifact_sha256",
                    "artifact_size",
                    "config",
                    "environment",
                    "safety_scan",
                ],
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "base_model_digest": {"type": "string"},
                    "tokenizer_digest": {"type": "string"},
                    "artifact_key": {"type": "string"},
                    "artifact_sha256": {"type": "string"},
                    "artifact_size": {"type": "integer"},
                    "config": {"type": "object"},
                    "environment": {"type": "object"},
                    "safety_scan": {"type": "object"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_create_release_candidate",
            handler=h5_create_release_candidate,
            schema={
                "type": "object",
                "required": ["manifest"],
                "properties": {"manifest": {"type": "object"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_advance_release",
            handler=h5_advance_release,
            schema={
                "type": "object",
                "required": ["release_id", "target"],
                "properties": {
                    "release_id": {"type": "string"},
                    "target": {"type": "string"},
                    "expected_version": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    registry.register(
        ToolSpec(
            name="h5_observe_release",
            handler=h5_observe_release,
            schema={
                "type": "object",
                "required": ["release_id", "metrics"],
                "properties": {
                    "release_id": {"type": "string"},
                    "metrics": {"type": "object"},
                },
                "additionalProperties": False,
            },
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            uses_identity=True,
            scope_resolver=_h5_scope,
            result_sensitivity={"*": "internal"},
        )
    )
    for name, kind in (("h5_train_lora", "lora_train"), ("h5_model_evaluate", "model_evaluate")):
        registry.register(
            ToolSpec(
                name=name,
                handler=lambda _arguments: (_ for _ in ()).throw(
                    RuntimeError("H5 Kubernetes job must not execute inline")
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
                uses_identity=True,
                scope_resolver=_h5_scope,
                execution="kubernetes_job",
                job_kind=kind,
                result_sensitivity={"*": "internal"},
            )
        )
