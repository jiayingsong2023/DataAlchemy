"""Independent EC4 evidence replay, including forged worker-success flags."""

import hashlib
from types import SimpleNamespace

import pytest
import torch
from safetensors.torch import save
from test_training_config import _config, _observed
from test_training_worker import _training_context

from core import verifier_training
from core.evidence import canonical_bytes, sha256
from core.verifiers import VerificationResult, default_verifiers


def _evidence(monkeypatch):
    context = _training_context()
    prefix = f"tenants/{context['tenant_id']}/"
    input_ref = prefix + "input.json"
    observation = _observed(_config())
    tensors = {
        f"base_model.model.{name}.lora_{side}.weight": torch.ones(
            (16, 8) if side == "A" else (8, 16)
        )
        for name in _config()["target_modules"]
        for side in ("A", "B")
    }
    files = {
        "adapter_config.json": canonical_bytes(observation["adapter_config"]),
        "adapter_model.safetensors": save(tensors),
    }
    digest = hashlib.sha256()
    for name, body in sorted(files.items()):
        digest.update(name.encode())
        digest.update(body)
    artifact_hash = digest.hexdigest()
    cost = {
        "schema_version": "training_cost_receipt.v1",
        **{
            key: context[key]
            for key in (
                "tenant_id",
                "adapter_id",
                "snapshot_id",
                "base_model_digest",
                "dataset_sha256",
            )
        },
        "artifact_sha256": artifact_hash,
        "started_at": "2026-09-19T00:00:00+00:00",
        "completed_at": "2026-09-19T00:00:02+00:00",
        "metrics": {
            "wall_time_seconds": 2.0,
            "gpu_model": "test",
            "gpu_count": 1,
            "steps": _config()["max_steps"],
            "processed_tokens": 20,
            "peak_vram_bytes": 1024,
            "gpu_seconds": 2.0,
            "normalized_cost": 2 / 3600,
        },
        "policy": context["training_cost_policy"],
    }
    bodies = {input_ref: canonical_bytes(context), prefix + "cost.json": canonical_bytes(cost)}
    source = {"ref": input_ref, "sha256": sha256(bodies[input_ref])}
    args = {"input_key": input_ref, "input_sha256": source["sha256"]}
    step = {"step_id": "step", "tool": "h5_train_lora", "arguments": args}
    approved = {
        "step_id": "step",
        "run_id": context["run_id"],
        "task_id": "task",
        "plan_version": 1,
        "tool": "h5_train_lora",
        "arguments_sha256": sha256(args),
        "by": "reviewer",
    }
    result_ref = prefix + "result.json"
    bodies[result_ref] = canonical_bytes(
        {
            "job_id": "job",
            "input_key": input_ref,
            "input_sha256": source["sha256"],
            "tool_result": {
                "output": {key: context[key] for key in ("adapter_id", "run_id", "snapshot_id")}
            },
        }
    )
    job = {
        "state": "succeeded",
        "tenant_id": context["tenant_id"],
        "run_id": context["run_id"],
        "task_id": "task",
        "step_id": "step",
        "input_key": input_ref,
        "input_sha256": source["sha256"],
        "plan_json": [step],
        "plan_version": 1,
        "requested_at": "2026-09-19T00:00:01+00:00",
        "completed_at": "2026-09-19T00:00:02+00:00",
        "approvals": [
            {
                "event_id": "event",
                "event_type": "approval_granted",
                "occurred_at": "2026-09-19T00:00:00+00:00",
                "payload_json": approved,
            }
        ],
        "result_key": result_ref,
        "result_sha256": sha256(bodies[result_ref]),
    }
    adapter = {
        **{
            key: context[key]
            for key in (
                "tenant_id",
                "adapter_id",
                "snapshot_id",
                "base_model_digest",
                "tokenizer_digest",
            )
        },
        "artifact_key": context["output_prefix"],
        "artifact_sha256": artifact_hash,
        "artifact_size": sum(map(len, files.values())),
        "config_json": {
            "training_input": source,
            "training_job_id": "job",
            "configuration_observation": observation,
            "execution_fingerprint": {
                "code_sha256": context["training_code_sha256"],
                "image_digest": context["training_image_digest"],
            },
            "training_cost_receipt": {
                "ref": prefix + "cost.json",
                "sha256": sha256(bodies[prefix + "cost.json"]),
            },
        },
    }
    services = SimpleNamespace(
        adapter=lambda _: adapter,
        object_body=bodies.get,
        training_job=lambda _: job,
        object_files=lambda _: files,
        snapshot=lambda _: {
            **context,
            "state": "approved",
            "algorithm": "sft",
            "target_tokenizer_digest": context["tokenizer_digest"],
            "compile_manifest_key": context["compile_manifest_ref"],
        },
    )
    monkeypatch.setattr(
        verifier_training, "_compile_manifest", lambda *_: VerificationResult("passed", {})
    )
    return context, adapter, job, bodies, files, services


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "input",
        "artifact",
        "extra",
        "observation",
        "approval",
        "job",
        "cost",
        "image",
        "tenant",
        "worker_pass",
        "late_approval",
        "cancel",
        "snapshot",
        "plan_input",
        "no_update",
    ],
)
def test_independent_configuration_replay(monkeypatch, mutation):  # noqa: C901 - adversarial matrix
    context, adapter, job, bodies, files, services = _evidence(monkeypatch)
    if mutation == "input":
        bodies[adapter["config_json"]["training_input"]["ref"]] = b"{}"
    elif mutation == "artifact":
        files["adapter_config.json"] = b"{}"
    elif mutation == "extra":
        files["extra.json"] = b"{}"
    elif mutation == "observation":
        adapter["config_json"]["configuration_observation"]["trainer_config"]["seed"] = 0
    elif mutation == "approval":
        job["approvals"][0]["payload_json"]["arguments_sha256"] = "b" * 64
    elif mutation == "job":
        job["state"] = "running"
    elif mutation == "cost":
        bodies[adapter["config_json"]["training_cost_receipt"]["ref"]] = b"{}"
    elif mutation == "image":
        adapter["config_json"]["execution_fingerprint"]["image_digest"] = "sha256:" + "b" * 64
    elif mutation == "tenant":
        adapter["tenant_id"] = "other"
    elif mutation == "worker_pass":
        adapter["config_json"]["configuration_verification"] = {"configuration_consistent": True}
        job["approvals"] = []
    elif mutation == "late_approval":
        job["approvals"][0]["occurred_at"] = "2026-09-19T00:00:03+00:00"
    elif mutation == "cancel":
        job["approvals"].append(
            {
                "event_type": "control_requested",
                "occurred_at": job["requested_at"],
                "payload_json": {"control": "cancel"},
            }
        )
    elif mutation == "snapshot":
        original = services.snapshot(None)
        services.snapshot = lambda _: {**original, "dataset_sha256": "f" * 64}
    elif mutation == "plan_input":
        job["plan_json"][0]["arguments"]["input_key"] = "other"
    elif mutation == "no_update":
        from safetensors.torch import load

        tensors = load(files["adapter_model.safetensors"])
        files["adapter_model.safetensors"] = save(
            {name: tensor.zero_() for name, tensor in tensors.items()}
        )
        digest = hashlib.sha256()
        for name, body in sorted(files.items()):
            digest.update(name.encode())
            digest.update(body)
        adapter["artifact_sha256"] = digest.hexdigest()
    verified = verifier_training._training_configuration(
        {"parameters": {"adapter_id": "adapter"}}, {"tenant_id": context["tenant_id"]}, {}, services
    )
    assert verified.status == ("passed" if mutation == "none" else "failed")
    if mutation == "none":
        assert verified.summary["independent_configuration_verification"] is True
        assert verified.summary["gpu_execution_verified"] is False
        assert verified.summary["artifact_sha256"] == adapter["artifact_sha256"]
        assert len(verified.summary["files"]) == 2


def test_runtime_result_envelope(monkeypatch):
    context, _, _, _, _, services = _evidence(monkeypatch)
    verified = verifier_training._training_configuration(
        {},
        {"tenant_id": context["tenant_id"]},
        {"output": {"output": {"adapter_id": context["adapter_id"]}}},
        services,
    )
    assert verified.status == "passed"


def test_registry_has_versioned_training_configuration_verifier():
    assert (
        default_verifiers().get("verify_training_configuration", 1).handler
        is verifier_training._training_configuration
    )


@pytest.mark.parametrize("status,exit_code", [("passed", 0), ("failed", 1)])
def test_replay_cli_exit_code(monkeypatch, capsys, status, exit_code):
    from scripts import verify_training_configuration as replay

    monkeypatch.setattr("sys.argv", ["replay", "--adapter-id", "adapter", "--tenant-id", "tenant"])
    monkeypatch.setenv("VERIFIER_DATABASE_URL", "read-only-test")
    monkeypatch.setattr(replay, "ReadOnlyServices", lambda *_: None)
    monkeypatch.setattr(
        replay,
        "default_verifiers",
        lambda: SimpleNamespace(
            get=lambda *_: SimpleNamespace(handler=lambda *_: VerificationResult(status, {}))
        ),
    )
    assert replay.main() == exit_code
    assert status in capsys.readouterr().out
