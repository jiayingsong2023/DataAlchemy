"""Training, deployment, and corpus verifiers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from src.harness.deployment import DeploymentBinding, validate_shadow_output

from .verifier_contracts import ReadOnlyServices, VerificationResult
from .verifier_evaluation import _compile_manifest, _model_migration


def _training_configuration(  # noqa: C901 - keep independent evidence checks linear
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Re-read approval, frozen input and artifact bytes, never trust a worker PASS flag."""
    from core.evidence import canonical_bytes, sha256
    from harness.jobs import validate_training_context
    from harness.model_migration import validate_training_cost_receipt
    from harness.training_config import verify_training_config_observation

    output = result.get("output", {})
    adapter_id = criterion.get("parameters", {}).get("adapter_id") or output.get(
        "output", output
    ).get("adapter_id")
    adapter = services.adapter(adapter_id) if isinstance(adapter_id, str) else None
    if not adapter or adapter["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "training_configuration_adapter_missing")
    try:
        config = adapter["config_json"]
        source = config["training_input"]
        tenant_prefix = f"tenants/{task['tenant_id']}/"
        if not source["ref"].startswith(tenant_prefix) or not adapter["artifact_key"].startswith(
            tenant_prefix
        ):
            raise ValueError("training_configuration_scope_mismatch")
        body = services.object_body(source["ref"])
        if body is None or sha256(body) != source["sha256"]:
            raise ValueError("training_configuration_input_hash_mismatch")
        context = validate_training_context(json.loads(body), for_execution=True)
        snapshot = services.snapshot(context["snapshot_id"])
        if not snapshot or snapshot["state"] != "approved":
            raise ValueError("training_configuration_snapshot_unapproved")
        bindings = {
            "tenant_id": "tenant_id",
            "dataset_key": "dataset_key",
            "dataset_sha256": "dataset_sha256",
            "base_model_digest": "base_model_digest",
            "tokenizer_digest": "target_tokenizer_digest",
            "chat_template_digest": "chat_template_digest",
            "compile_manifest_ref": "compile_manifest_key",
            "compile_manifest_sha256": "compile_manifest_sha256",
        }
        if snapshot["algorithm"] != "sft" or any(
            context[key] != snapshot[field] for key, field in bindings.items()
        ):
            raise ValueError("training_configuration_snapshot_binding_mismatch")
        if (
            context["tenant_id"] != adapter["tenant_id"]
            or context["adapter_id"] != str(adapter["adapter_id"])
            or context["snapshot_id"] != str(adapter["snapshot_id"])
            or context["base_model_digest"] != adapter["base_model_digest"]
            or context["tokenizer_digest"] != adapter["tokenizer_digest"]
            or context["output_prefix"] != adapter["artifact_key"]
        ):
            raise ValueError("training_configuration_context_mismatch")
        job = services.training_job(config["training_job_id"])
        if (
            not job
            or job["state"] != "succeeded"
            or job["tenant_id"] != context["tenant_id"]
            or str(job["run_id"]) != context["run_id"]
            or job["input_key"] != source["ref"]
            or job["input_sha256"] != source["sha256"]
        ):
            raise ValueError("training_configuration_job_mismatch")
        step = next(
            (item for item in job["plan_json"] if item["step_id"] == str(job["step_id"])), None
        )
        if (
            not step
            or step["tool"] != "h5_train_lora"
            or step["arguments"].get("input_sha256") != source["sha256"]
            or step["arguments"].get("input_key") != source["ref"]
        ):
            raise ValueError("training_configuration_plan_mismatch")
        approvals = [
            event
            for event in job["approvals"]
            if (
                event["event_type"] == "approval_granted"
                and event["payload_json"].get("step_id") == str(job["step_id"])
                and event["payload_json"].get("run_id") == str(job["run_id"])
                and event["payload_json"].get("task_id") == str(job["task_id"])
                and event["payload_json"].get("plan_version") == job["plan_version"]
                and event["payload_json"].get("tool") == "h5_train_lora"
                and event["payload_json"].get("arguments_sha256") == sha256(step["arguments"])
                and event["payload_json"].get("by")
            )
        ]
        if len(approvals) != 1:
            raise ValueError("training_configuration_approval_ambiguous")
        approved_at = datetime.fromisoformat(str(approvals[0]["occurred_at"]))
        requested_at = datetime.fromisoformat(str(job["requested_at"]))
        completed_at = datetime.fromisoformat(str(job["completed_at"]))
        if not approved_at <= requested_at <= completed_at or any(
            approved_at <= datetime.fromisoformat(str(event["occurred_at"])) <= completed_at
            and (
                event["event_type"]
                in {"approval_rejected", "replanned", "cancelled", "job_cancelled"}
                or event["payload_json"].get("control") == "cancel"
            )
            for event in job["approvals"]
        ):
            raise ValueError("training_configuration_approval_timing_invalid")
        result_body = services.object_body(job["result_key"])
        if result_body is None or sha256(result_body) != job["result_sha256"]:
            raise ValueError("training_configuration_job_result_hash_mismatch")
        recorded = json.loads(result_body)
        if (
            recorded["job_id"] != config["training_job_id"]
            or recorded["input_key"] != source["ref"]
            or recorded["input_sha256"] != source["sha256"]
            or any(
                recorded["tool_result"]["output"][key] != context[key]
                for key in ("adapter_id", "run_id", "snapshot_id")
            )
        ):
            raise ValueError("training_configuration_job_result_mismatch")
        files = services.object_files(adapter["artifact_key"])
        if "adapter_config.json" not in files or "adapter_model.safetensors" not in files:
            raise ValueError("training_configuration_artifact_missing")
        if set(files) != {"adapter_config.json", "adapter_model.safetensors"}:
            raise ValueError("training_configuration_unexpected_artifact")
        digest = hashlib.sha256()
        for name, value in sorted(files.items()):
            digest.update(name.encode())
            digest.update(value)
        if (
            digest.hexdigest() != adapter["artifact_sha256"]
            or sum(map(len, files.values())) != adapter["artifact_size"]
        ):
            raise ValueError("training_configuration_artifact_hash_mismatch")
        observation = {
            **config["configuration_observation"],
            "adapter_config": json.loads(files["adapter_config.json"]),
        }
        verified = verify_training_config_observation(context, **observation)
        if config["execution_fingerprint"] != {
            "code_sha256": context["training_code_sha256"],
            "image_digest": context["training_image_digest"],
        }:
            raise ValueError("training_configuration_execution_fingerprint_mismatch")
        from safetensors.torch import load

        tensors = load(files["adapter_model.safetensors"])
        expected = {
            f"base_model.model.{name}.lora_{side}.weight": side
            for name in context["effective_training_config"]["target_modules"]
            for side in ("A", "B")
        }
        if tensors.keys() != expected.keys() or any(
            tensor.ndim != 2
            or tensor.shape[0 if expected[name] == "A" else 1]
            != context["effective_training_config"]["r"]
            or not bool(tensor.isfinite().all())
            for name, tensor in tensors.items()
        ):
            raise ValueError("training_configuration_tensor_contract_mismatch")
        if not any(
            bool(tensor.count_nonzero())
            for name, tensor in tensors.items()
            if expected[name] == "B"
        ):
            raise ValueError("training_configuration_no_weight_update")
        cost_ref = config["training_cost_receipt"]
        if not cost_ref["ref"].startswith(tenant_prefix):
            raise ValueError("training_configuration_cost_scope_mismatch")
        cost_body = services.object_body(cost_ref["ref"])
        if cost_body is None or sha256(cost_body) != cost_ref["sha256"]:
            raise ValueError("training_configuration_cost_hash_mismatch")
        cost = validate_training_cost_receipt(json.loads(cost_body))
        if (
            any(
                cost[key] != context[key]
                for key in (
                    "tenant_id",
                    "snapshot_id",
                    "adapter_id",
                    "base_model_digest",
                    "dataset_sha256",
                )
            )
            or cost["artifact_sha256"] != adapter["artifact_sha256"]
            or cost["metrics"]["steps"] != context["effective_training_config"]["max_steps"]
        ):
            raise ValueError("training_configuration_cost_binding_mismatch")
        compiled = _compile_manifest(
            {
                "parameters": {
                    "snapshot_id": context["snapshot_id"],
                    "compile_manifest_ref": context["compile_manifest_ref"],
                    "compile_manifest_sha256": context["compile_manifest_sha256"],
                }
            },
            task,
            {},
            services,
        )
        if compiled.status != "passed":
            return compiled
        return VerificationResult(
            "passed",
            {
                "schema_version": "training_configuration_verification.v1",
                **verified,
                "independent_configuration_verification": True,
                "gpu_execution_verified": False,
                "adapter_id": str(adapter["adapter_id"]),
                "input_sha256": source["sha256"],
                "artifact_sha256": adapter["artifact_sha256"],
                "execution_fingerprint": config["execution_fingerprint"],
                "approval_event_id": str(approvals[0]["event_id"]),
                "approval_sha256": sha256(canonical_bytes(approvals[0]["payload_json"])),
                "observation_sha256": sha256(observation),
                "cost_receipt_sha256": cost_ref["sha256"],
                "files": [
                    {"path": name, "sha256": sha256(value), "size": len(value)}
                    for name, value in sorted(files.items())
                ],
            },
        )
    except (ValueError, KeyError, TypeError, StopIteration) as error:
        return VerificationResult("failed", {}, str(error))


def _dpo_gate(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Verify that DPO remains disabled when its upstream evidence is insufficient."""
    from harness.model_migration import (
        build_dpo_gate_decision,
        validate_dpo_gate_decision,
        validate_migration_report,
    )

    parameters = criterion.get("parameters", {})
    ref = parameters.get("decision_ref")
    expected_sha256 = parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "dpo_gate_hash_mismatch")
    try:
        decision = validate_dpo_gate_decision(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "dpo_gate_invalid")
    if decision["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "dpo_gate_tenant_mismatch")

    migration_source = decision["migration_report"]
    migration_body = services.object_body(migration_source["ref"])
    if (
        migration_body is None
        or hashlib.sha256(migration_body).hexdigest() != migration_source["sha256"]
    ):
        return VerificationResult("failed", {}, "dpo_gate_migration_hash_mismatch")
    migration_verified = _model_migration(
        {
            "parameters": {
                "report_ref": migration_source["ref"],
                "report_sha256": migration_source["sha256"],
            }
        },
        task,
        {},
        services,
    )
    if migration_verified.status != "passed":
        return VerificationResult("failed", {}, "dpo_gate_migration_unverified")
    try:
        migration = validate_migration_report(json.loads(migration_body))
        rebuilt = build_dpo_gate_decision(
            tenant_id=decision["tenant_id"],
            migration_report=migration,
            migration_report_ref=migration_source["ref"],
            migration_report_sha256=migration_source["sha256"],
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "dpo_gate_not_reproducible")
    if rebuilt != decision:
        return VerificationResult("failed", {}, "dpo_gate_not_reproducible")
    return VerificationResult("passed", decision["decision"])


def _rl_gate(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Verify that RL and Agent Lightning remain disabled without prerequisite evidence."""
    from harness.model_migration import (
        build_rl_gate_decision,
        validate_dpo_gate_decision,
        validate_rl_gate_decision,
    )

    parameters = criterion.get("parameters", {})
    ref = parameters.get("decision_ref")
    expected_sha256 = parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "rl_gate_hash_mismatch")
    try:
        decision = validate_rl_gate_decision(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "rl_gate_invalid")
    if decision["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "rl_gate_tenant_mismatch")

    dpo_source = decision["dpo_gate_decision"]
    dpo_body = services.object_body(dpo_source["ref"])
    if dpo_body is None or hashlib.sha256(dpo_body).hexdigest() != dpo_source["sha256"]:
        return VerificationResult("failed", {}, "rl_gate_dpo_hash_mismatch")
    dpo_verified = _dpo_gate(
        {
            "parameters": {
                "decision_ref": dpo_source["ref"],
                "decision_sha256": dpo_source["sha256"],
            }
        },
        task,
        {},
        services,
    )
    if dpo_verified.status != "passed":
        return VerificationResult("failed", {}, "rl_gate_dpo_unverified")
    try:
        dpo = validate_dpo_gate_decision(json.loads(dpo_body))
        rebuilt = build_rl_gate_decision(
            tenant_id=decision["tenant_id"],
            dpo_gate_decision=dpo,
            dpo_gate_decision_ref=dpo_source["ref"],
            dpo_gate_decision_sha256=dpo_source["sha256"],
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "rl_gate_not_reproducible")
    if rebuilt != decision:
        return VerificationResult("failed", {}, "rl_gate_not_reproducible")
    return VerificationResult(
        "passed", {**decision["decision"], "agent_lightning": decision["agent_lightning"]}
    )


def _training_snapshot(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    snapshot_id = criterion.get("parameters", {}).get("snapshot_id")
    snapshot = services.snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    if snapshot is None or snapshot["state"] != "approved":
        return VerificationResult("failed", {}, "snapshot_not_approved")
    items = snapshot.get("items", [])
    if not items or not all(item["training_allowed"] for item in items):
        return VerificationResult("failed", {}, "snapshot_training_permission_missing")
    if {item["split"] for item in items} != {"train", "validation"}:
        return VerificationResult("failed", {}, "snapshot_split_invalid")
    if any(item["source_tenant_id"] != snapshot["tenant_id"] for item in items):
        return VerificationResult("failed", {}, "snapshot_source_tenant_mismatch")
    return VerificationResult(
        "passed", {"snapshot_id": str(snapshot["snapshot_id"]), "items": len(items)}
    )


def _base_evaluation(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    evaluation_id = criterion.get("parameters", {}).get("evaluation_id")
    evaluation = services.evaluation(evaluation_id) if isinstance(evaluation_id, str) else None
    if (
        evaluation is None
        or evaluation["subject_type"] != "base"
        or evaluation["state"] != "passed"
    ):
        return VerificationResult("failed", {}, "base_evaluation_not_passed")
    gates = evaluation.get("hard_gates", {})
    if gates.get("passed") is not True or gates.get("invalidated_trials", 0):
        return VerificationResult("failed", {}, "base_evaluation_gate_failed")
    return VerificationResult("passed", {"evaluation_id": str(evaluation["evaluation_id"])})


def _training_input(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    parameters = criterion.get("parameters", {})
    snapshot = services.snapshot(parameters.get("snapshot_id"))
    base = services.evaluation(parameters.get("base_evaluation_id"))
    if snapshot is None or snapshot["state"] != "approved":
        return VerificationResult("failed", {}, "training_snapshot_not_ready")
    if base is None or base["state"] != "passed" or base["subject_type"] != "base":
        return VerificationResult("failed", {}, "training_base_evaluation_missing")
    if snapshot["base_model_digest"] != parameters.get("base_model_digest"):
        return VerificationResult("failed", {}, "training_base_model_mismatch")
    if snapshot.get("algorithm") == "sft":
        compiled = _compile_manifest(criterion, _task, _result, services)
        if compiled.status != "passed":
            return compiled
    return VerificationResult(
        "passed",
        {
            "snapshot_id": str(snapshot["snapshot_id"]),
            "base_evaluation_id": str(base["evaluation_id"]),
        },
    )


def _adapter(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    adapter_id = criterion.get("parameters", {}).get("adapter_id")
    adapter = services.adapter(adapter_id) if isinstance(adapter_id, str) else None
    if adapter is None or adapter["state"] != "verified":
        return VerificationResult("failed", {}, "adapter_not_verified")
    if adapter["safety_scan_json"].get("passed") is not True:
        return VerificationResult("failed", {}, "adapter_safety_scan_failed")
    config = adapter["config_json"]
    if config.get("format") not in {"safetensors", "safetensors+json"}:
        return VerificationResult("failed", {}, "adapter_format_not_allowed")
    return VerificationResult("passed", {"adapter_id": str(adapter["adapter_id"])})


def _evaluation(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    evaluation_id = criterion.get("parameters", {}).get("evaluation_id")
    evaluation = services.evaluation(evaluation_id) if isinstance(evaluation_id, str) else None
    if evaluation is None or evaluation["state"] != "passed":
        return VerificationResult("failed", {}, "evaluation_not_passed")
    gates = evaluation.get("hard_gates", {})
    if gates.get("passed") is not True or gates.get("invalidated_trials", 0):
        return VerificationResult("failed", {}, "evaluation_hard_gate_failed")
    if gates.get("judge_only") is True:
        return VerificationResult("failed", {}, "judge_cannot_be_release_gate")
    return VerificationResult("passed", {"evaluation_id": str(evaluation["evaluation_id"])})


def _release_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion.get("parameters", {}).get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    manifest = row["manifest_json"] if row else {}
    required = {"adapter_id", "evaluation_id", "training_snapshot_id", "rollback_to", "guardrails"}
    if row is None or row["status"] not in {"candidate", "shadow", "canary", "promoted"}:
        return VerificationResult("failed", {}, "release_not_active")
    if not required <= manifest.keys() or manifest.get("evaluation", {}).get("passed") is not True:
        return VerificationResult("failed", {}, "release_manifest_incomplete")
    if row.get("release_scope") != "single_tenant_lora":
        return VerificationResult("failed", {}, "release_scope_unsupported")
    return VerificationResult(
        "passed", {"release_id": str(row["release_id"]), "status": row["status"]}
    )


def _qualification(  # noqa: C901 - independent evidence checks stay linear
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    qualification_id = criterion.get("parameters", {}).get("qualification_id")
    expected_state = criterion.get("parameters", {}).get("expected_state", "calibrated")
    if not isinstance(qualification_id, str) or expected_state not in {
        "data_approved",
        "calibrated",
        "pilot_ready",
    }:
        return VerificationResult("failed", {}, "qualification_parameters_invalid")
    row = services.qualification(qualification_id)
    if row is None:
        return VerificationResult("failed", {}, "qualification_not_found")
    if row["state"] != expected_state:
        return VerificationResult("failed", {"state": row["state"]}, "qualification_state_mismatch")
    if (
        not row["source_manifest_key"]
        or not row["source_acl_digest"]
        or not row["permission_version"]
    ):
        return VerificationResult("failed", {}, "qualification_provenance_missing")
    for key in ("source_manifest_sha256", "suite_sha256"):
        value = row[key]
        if not isinstance(value, str) or len(value) != 64:
            return VerificationResult("failed", {}, f"qualification_{key}_invalid")
    if expected_state in {"calibrated", "pilot_ready"}:
        if (
            not row["reviewer"]
            or row["reviewer"] == row["created_by"]
            or not row["base_evaluation_id"]
            or not row["candidate_evaluation_id"]
            or not row["calibration_report_key"]
            or not row["calibration_report_sha256"]
        ):
            return VerificationResult("failed", {}, "qualification_calibration_incomplete")
    if expected_state == "pilot_ready":
        if (
            not row["stable_release_id"]
            or not row["candidate_release_id"]
            or not row["deployment_evidence_key"]
            or not row["deployment_evidence_sha256"]
        ):
            return VerificationResult("failed", {}, "qualification_deployment_incomplete")
    return VerificationResult(
        "passed", {"qualification_id": qualification_id, "state": row["state"]}
    )


def _qualification_manifest_artifacts(
    manifest: dict[str, Any], services: ReadOnlyServices
) -> VerificationResult | None:
    tenant_id = manifest["data_scope"]["tenant_id"]
    artifacts = (
        (
            "source_manifest",
            manifest["data_scope"]["source_manifest_ref"],
            manifest["data_scope"]["source_manifest_sha256"],
            "rtd_q0_source_manifest.v1",
        ),
        (
            "suite",
            manifest["suite"]["ref"],
            manifest["suite"]["sha256"],
            "rtd_q0_qualification_suite.v1",
        ),
    )
    loaded = {}
    for name, artifact_ref, artifact_sha256, schema_version in artifacts:
        artifact_body = services.object_body(artifact_ref)
        if artifact_body is None or hashlib.sha256(artifact_body).hexdigest() != artifact_sha256:
            return VerificationResult("failed", {}, f"qualification_{name}_hash_mismatch")
        try:
            artifact = json.loads(artifact_body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return VerificationResult("failed", {}, f"qualification_{name}_invalid")
        if (
            artifact.get("schema_version") != schema_version
            or artifact.get("tenant_id") != tenant_id
        ):
            return VerificationResult("failed", {}, f"qualification_{name}_invalid")
        loaded[name] = artifact
    if loaded["suite"].get("source_manifest") != {
        "ref": manifest["data_scope"]["source_manifest_ref"],
        "sha256": manifest["data_scope"]["source_manifest_sha256"],
    }:
        return VerificationResult("failed", {}, "qualification_suite_source_mismatch")
    return None


def _qualification_manifest(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    from harness.qualification import validate_qualification_manifest

    parameters = criterion.get("parameters", {})
    ref = parameters.get("manifest_ref")
    expected_sha256 = parameters.get("manifest_sha256")
    expected_state = parameters.get("expected_state", "frozen")
    if not isinstance(ref, str) or expected_state not in {"draft", "frozen"}:
        return VerificationResult("failed", {}, "qualification_manifest_parameters_invalid")
    body = services.object_body(ref)
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "qualification_manifest_hash_mismatch")
    try:
        manifest = validate_qualification_manifest(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "qualification_manifest_invalid")
    if manifest["state"] != expected_state:
        return VerificationResult(
            "failed", {"state": manifest["state"]}, "qualification_manifest_state_mismatch"
        )
    tenant_id = manifest["data_scope"]["tenant_id"]
    if expected_state == "frozen" and tenant_id != task.get("tenant_id"):
        return VerificationResult("failed", {}, "qualification_manifest_tenant_mismatch")
    if expected_state == "frozen" and (
        artifact_error := _qualification_manifest_artifacts(manifest, services)
    ):
        return artifact_error
    return VerificationResult(
        "passed",
        {
            "state": manifest["state"],
            "version": manifest["version"],
            "claim_mode": manifest["product_claim"]["mode"],
            "blockers": manifest["blockers"],
        },
    )


def _deployment_binding(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion.get("parameters", {}).get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    try:
        binding = DeploymentBinding.from_manifest(row["manifest_json"] if row else {})
    except (TypeError, ValueError) as error:
        return VerificationResult("failed", {}, str(error))
    if row["status"] not in {"shadow", "canary", "promoted"}:
        return VerificationResult("failed", {}, "deployment_release_not_active")
    if result.get("output", {}).get("candidate_release_id") != binding.candidate_release_id:
        return VerificationResult("failed", {}, "deployment_candidate_mismatch")
    return VerificationResult(
        "passed", {"mode": binding.mode, "canary_percent": binding.canary_percent}
    )


def _shadow(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    _services: ReadOnlyServices,
) -> VerificationResult:
    try:
        validate_shadow_output(result.get("output", {}))
    except ValueError as error:
        return VerificationResult("failed", {}, str(error))
    return VerificationResult("passed", {"authority": "stable"})


def _rough_clean(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    step_id = result.get("step_id") or task["plan"][task["current_step"]]["step_id"]
    job = services.job(task["task_id"], step_id)
    artifact = next(
        (
            item
            for item in result.get("artifacts", [])
            if item.get("store") == "minio" and item.get("kind") == "cleaned_corpus"
        ),
        None,
    )
    if job is None or job["state"] != "succeeded" or not job["result_sha256"]:
        return VerificationResult("failed", {}, "job_result_unverified")
    if (
        artifact is None
        or not isinstance(artifact.get("sha256"), str)
        or len(artifact["sha256"]) != 64
    ):
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    if result.get("observed_scope") != [f"raw:{job['input_key']}"]:
        return VerificationResult("failed", {}, "job_scope_mismatch")
    return VerificationResult("passed", {"job_result_sha256": job["result_sha256"]})


def _rough_clean_v2(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    outcome = _rough_clean(criterion, task, result, services)
    if outcome.status != "passed":
        return outcome
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "cleaned_corpus"), None
    )
    if artifact is None:
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    # Spark writes one output prefix containing several products; rough-clean
    # schema verification must read only the cleaned-corpus product, not the
    # later RAG rows whose shape is intentionally different.
    records = services.object_records(artifact["id"].rstrip("/") + "/cleaned_corpus.jsonl")
    if not records:
        return VerificationResult("failed", {}, "rough_records_missing")
    accepted = 0
    for record in records:
        required = {
            "text",
            "source_uri",
            "source_version",
            "tenant_id",
            "acl_digest",
            "trust_label",
            "decision",
        }
        if not required <= record.keys() or record["tenant_id"] != task["tenant_id"]:
            return VerificationResult("failed", {}, "rough_schema_invalid")
        if record["decision"] == "accepted":
            accepted += 1
    if accepted < 1:
        return VerificationResult("failed", {}, "rough_no_accepted_records")
    return VerificationResult("passed", {"records": len(records), "accepted": accepted})


def _artifact_json(
    result: dict[str, Any], services: ReadOnlyServices, kind: str
) -> tuple[dict[str, Any] | None, str | None]:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == kind), None
    )
    if artifact is None:
        return None, "content_projection_artifact_missing"
    body = services.object_body(artifact["id"])
    if body is None or hashlib.sha256(body).hexdigest() != artifact["sha256"]:
        return None, f"{kind}_hash_mismatch"
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, f"{kind}_schema_invalid"


def _refined_corpus(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    canonical, error = _artifact_json(result, services, "canonical_content")
    if error:
        return VerificationResult("failed", {}, error)
    assert canonical is not None
    if canonical.get("tenant_id") != task["tenant_id"] or not canonical.get("documents"):
        return VerificationResult("failed", {}, "canonical_schema_invalid")
    span_ids: set[str] = set()
    for document in canonical["documents"]:
        if not document.get("acl_digest") or document.get("trust_label") != "untrusted_external":
            return VerificationResult("failed", {}, "canonical_lineage_missing")
        spans = document.get("spans", [])
        if not spans or any(not span.get("text") or not span.get("locator") for span in spans):
            return VerificationResult("failed", {}, "canonical_spans_empty")
        span_ids.update(span["span_id"] for span in spans)

    projection, error = _artifact_json(result, services, "rag_projection")
    if error:
        return VerificationResult("failed", {}, error)
    assert projection is not None
    if projection.get("tenant_id") != task["tenant_id"] or not projection.get("documents"):
        return VerificationResult("failed", {}, "rag_projection_schema_invalid")
    chunks = [chunk for document in projection["documents"] for chunk in document.get("chunks", [])]
    if not chunks or any(
        not chunk.get("retrieval_text")
        or not chunk.get("source_span_ids")
        or not set(chunk["source_span_ids"]) <= span_ids
        for chunk in chunks
    ):
        return VerificationResult("failed", {}, "rag_projection_lineage_invalid")
    return VerificationResult(
        "passed", {**canonical.get("metrics", {}), **projection.get("metrics", {})}
    )


def _conflict_report(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "conflict_report"),
        None,
    )
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_report_missing")
    report = services.object_json(artifact["id"])
    if not isinstance(report, dict) or not report.get("candidates") or "decision" not in report:
        return VerificationResult("failed", {}, "source_evidence_missing")
    if any(
        not {"source_uri", "source_version", "acl_digest", "candidate_id"} <= candidate.keys()
        for candidate in report["candidates"]
    ):
        return VerificationResult("failed", {}, "source_evidence_missing")
    return VerificationResult(
        "passed",
        {"status": report["decision"].get("status"), "candidates": len(report["candidates"])},
    )


def _conflict_decision(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "conflict_decision"),
        None,
    )
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_decision_missing")
    decision = services.object_json(artifact["id"])
    if (
        not isinstance(decision, dict)
        or decision.get("decision", {}).get("status") != "resolved"
        or not decision["decision"].get("approved_by")
    ):
        return VerificationResult("failed", {}, "decision_unapproved")
    return VerificationResult(
        "passed", {"selected_candidate_id": decision["decision"].get("selected_candidate_id")}
    )
