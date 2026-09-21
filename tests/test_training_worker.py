"""EC4 creation/worker wiring tests; no training permission or GPU evidence."""

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_training_config import _config, _context, _environment, _observed

from core.evidence import sha256
from harness import job_runner
from harness.jobs import validate_training_context
from harness.training_config import (
    freeze_training_config,
    training_code_fingerprint,
    training_environment,
    training_kwargs,
)


def _training_context():
    context = {
        **_context(),
        "harness_version": 8,
        "run_id": "run",
        "username": "trainer",
        "role": "admin",
        "snapshot_state": "approved",
        "dataset_key": "tenants/ec4-synthetic/dataset.jsonl",
        "model_id": "/model",
        "model_metadata_sha256": "f" * 64,
        "training_code_sha256": training_code_fingerprint(),
        "training_image_digest": "sha256:" + "a" * 64,
        "database_url": "postgresql://example",
        "base_evaluation_id": "base",
        "base_evaluation_passed": True,
        "output_prefix": "tenants/ec4-synthetic/adapters/one",
        "compile_manifest_ref": "tenants/ec4-synthetic/compile.json",
        "adapter_id": "adapter",
        "training_cost_policy": {
            "version": "gpu-hour@1",
            "unit": "gpu_hour",
            "seconds_per_unit": 3600,
        },
    }
    return freeze_training_config(context, _config(), _environment())


def test_v8_is_explicit_and_legacy_is_read_only():
    context = _training_context()
    assert validate_training_context(context, for_execution=True) == context
    for version in (5, 6, 7):
        legacy = {**context, "harness_version": version}
        assert validate_training_context(legacy)["harness_version"] == version
        with pytest.raises(ValueError, match="legacy_context_read_only"):
            validate_training_context(legacy, for_execution=True)
    context["model_metadata_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="binding_mismatch"):
        validate_training_context(context, for_execution=True)


def test_real_peft_minimization_is_canonicalized_from_actual_injected_modules():
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import LlamaConfig, LlamaForCausalLM

    import train

    names = sorted(f"model.layers.{i}.self_attn.q_proj" for i in range(22))
    with torch.device("meta"):
        base = LlamaForCausalLM(
            LlamaConfig(
                hidden_size=8,
                intermediate_size=16,
                num_hidden_layers=22,
                num_attention_heads=2,
                num_key_value_heads=2,
                vocab_size=16,
            )
        )
    model = get_peft_model(base, LoraConfig(r=2, target_modules=names, task_type="CAUSAL_LM"))
    assert model.peft_config["default"].target_modules == {"q_proj"}
    assert train._exact_lora_modules(model, names) == names
    assert model.peft_config["default"].target_modules == set(names)
    with pytest.raises(ValueError, match="resolved_modules_mismatch"):
        train._exact_lora_modules(model, names[:-1])


def test_training_kwargs_ignore_environment_and_separate_preprocessing(monkeypatch):
    for key in ("MAX_LENGTH", "MAX_STEPS", "EVAL_STEPS", "BATCH_SIZE", "WARMUP_STEPS"):
        monkeypatch.setenv(f"H5_TRAIN_{key}", "999")
    lora, trainer = training_kwargs(_config())
    assert lora["r"] == 16 and trainer["max_steps"] == 50
    assert trainer["seed"] == 42 and trainer["optim"] == "adamw_torch"
    assert "max_length" not in trainer and "algorithm" not in trainer
    assert trainer["fp16"] is True and trainer["bf16"] is False


def _approval_row():
    arguments = {"input_key": "input", "input_sha256": "a" * 64}
    step = {"step_id": "step", "tool": "h5_train_lora", "arguments": arguments}
    return {
        "input_key": "input",
        "input_sha256": "a" * 64,
        "step_id": "step",
        "approval_json": {**step, "step": 0, "approved": True, "approved_by": "reviewer"},
        "state": "waiting_job",
        "plan_json": [copy.deepcopy(step)],
        "current_step": 0,
        "run_id": "run",
        "task_run_id": "run",
    }


@pytest.mark.parametrize(
    "mutation", ["none", "hash", "approval", "step", "plan", "cancelled", "missing", "run"]
)
def test_worker_rereads_exact_job_approval(monkeypatch, mutation):  # noqa: C901 - mutation matrix
    row = _approval_row()
    if mutation == "hash":
        row["approval_json"]["arguments"]["input_sha256"] = "b" * 64
    elif mutation == "approval":
        row["approval_json"]["approved"] = False
    elif mutation == "step":
        row["approval_json"]["step_id"] = "other"
    elif mutation == "plan":
        row["plan_json"][0]["arguments"]["input_key"] = "other"
    elif mutation == "cancelled":
        row["state"] = "cancelled"
    elif mutation == "missing":
        row = None
    elif mutation == "run":
        row["task_run_id"] = "other"
    cursor = Mock()
    cursor.fetchone.return_value = row

    @contextmanager
    def scope(*_args, **_kwargs):
        yield SimpleNamespace(cursor=lambda: scope_cursor())

    @contextmanager
    def scope_cursor():
        yield cursor

    monkeypatch.setattr(
        job_runner, "PostgresDatabase", lambda *_: SimpleNamespace(transaction=scope)
    )
    if mutation == "none":
        job_runner._training_approval(
            "db", {"tenant_id": "tenant"}, "job", "input", "a" * 64, "run"
        )
        assert "JOIN agent_tasks" in cursor.execute.call_args.args[0]
    else:
        with pytest.raises(ValueError, match="input_unapproved"):
            job_runner._training_approval(
                "db", {"tenant_id": "tenant"}, "job", "input", "a" * 64, "run"
            )


def test_legacy_pdf_cannot_submit_new_training():
    from scripts.run_h5_pdf_cycle import run_job

    service = Mock()
    with pytest.raises(RuntimeError, match="legacy_training_context_read_only"):
        run_job(
            service,
            {},
            {},
            kind="lora_train",
            root_run_id="run",
            attempt_id="attempt",
            gate_name="lora",
            input_key="input",
            input_sha256="a" * 64,
        )
    service.request.assert_not_called()


@pytest.mark.parametrize("failure", ["legacy", "environment", "dataset"])
def test_train_preflight_fails_before_loading_model(monkeypatch, failure):
    import train

    context = _training_context()
    store = Mock()
    store.get_object_body.return_value = b"changed"
    model_loader = Mock()
    monkeypatch.setattr(train, "S3Utils", lambda: store)
    monkeypatch.setattr(train, "validate_training_model", lambda *_: None)
    monkeypatch.setattr(train, "training_environment", _environment)
    monkeypatch.setattr(train.AutoModelForCausalLM, "from_pretrained", model_loader)
    if failure == "legacy":
        context["harness_version"] = 7
    elif failure == "environment":
        monkeypatch.setattr(train, "training_environment", lambda: {})
    with pytest.raises(ValueError):
        train.train(context)
    model_loader.assert_not_called()
    store.upload_directory.assert_not_called()


def test_compiled_creator_stops_for_hash_bound_approval(monkeypatch, tmp_path, capsys):
    from unittest.mock import AsyncMock

    from scripts import train_compiled_snapshot as creator

    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"config": _config(), "environment": _environment()}))
    monkeypatch.setattr(
        "sys.argv",
        [
            "train_compiled_snapshot",
            "--snapshot-id",
            "snapshot-1",
            "--base-evaluation-id",
            "base",
            "--model-id",
            "/model",
            "--model-dir",
            str(tmp_path),
            "--tenant-id",
            "ec4-synthetic",
            "--database-url",
            "db",
            "--job-database-url",
            "job-db",
            "--training-profile",
            str(profile),
        ],
    )
    context = _training_context()
    snapshot = {
        **context,
        "state": "approved",
        "algorithm": "sft",
        "items": [{"source_id": "annotation"}],
        "compile_manifest_key": context["compile_manifest_ref"],
        "target_tokenizer_digest": context["tokenizer_digest"],
    }
    services = Mock()
    services.snapshot.return_value = snapshot
    services.evaluation.return_value = {
        "subject_type": "base",
        "state": "passed",
        "required_trials": 1,
        "metrics": {"total": 1},
        "hard_gates": {"independent_verifier": True, "invalidated_trials": 0, "judge_only": False},
    }
    services.annotation.return_value = {"trial_id": "trial"}
    services.trial.return_value = {"task_id": "source-task"}
    monkeypatch.setattr(creator, "ReadOnlyServices", lambda *_: services)
    store = Mock()
    monkeypatch.setattr(creator, "S3Utils", lambda: store)
    runtime = Mock()

    def get_task(task_id, _identity):
        if task_id == "source-task":
            return {"run_id": "source-run"}
        raise PermissionError("Task not found")

    runtime.get_task.side_effect = get_task
    runtime.create_task.return_value = {"task_id": "training-task"}
    runtime.run = AsyncMock(return_value={"task_id": "training-task", "state": "waiting_approval"})
    monkeypatch.setattr(creator, "build_runtime", lambda *_: runtime)
    monkeypatch.setattr(creator, "register_release_tools", lambda _: None)

    def prepare(value, profile, _model_dir):
        return freeze_training_config(
            {**value, "harness_version": 8, "model_metadata_sha256": "f" * 64},
            profile["config"],
            profile["environment"],
        )

    monkeypatch.setattr(creator, "prepare_training_context", prepare)
    creator.main()
    request = json.loads(store.put_object.call_args.args[1])
    task_kwargs = runtime.create_task.call_args.kwargs
    plan = runtime.create_task.call_args.args[2]
    assert request["run_id"] == task_kwargs["run_id"] != "source-run"
    assert request["source_run_id"] == "source-run"
    assert plan[0]["arguments"]["input_sha256"] == sha256(store.put_object.call_args.args[1])
    assert json.loads(capsys.readouterr().out)["state"] == "waiting_approval"
    runtime.approve.assert_not_called()
    runtime.jobs.request.assert_not_called()


def test_creator_resolves_local_modules_without_weights_or_gpu(tmp_path, monkeypatch):
    import torch
    from transformers import LlamaConfig

    from harness.evaluation import model_path_fingerprint
    from harness.training_config import prepare_training_context

    monkeypatch.setenv("HARNESS_JOB_IMAGE", "sha256:" + "a" * 64)

    LlamaConfig(
        hidden_size=8,
        intermediate_size=16,
        num_hidden_layers=2,
        num_attention_heads=2,
        num_key_value_heads=2,
        vocab_size=16,
    ).save_pretrained(tmp_path)
    (tmp_path / "model.safetensors").write_bytes(b"not loaded by meta resolution")
    (tmp_path / "tokenizer.json").write_text("{}")
    (tmp_path / "tokenizer_config.json").write_text("{}")
    fingerprint = model_path_fingerprint(tmp_path, model_root=tmp_path)
    context = {
        **_training_context(),
        "base_model_digest": fingerprint["model_sha256"],
        "tokenizer_digest": fingerprint["tokenizer_sha256"],
        "chat_template_digest": fingerprint["chat_template_sha256"],
    }
    profile = {"config": _config(), "environment": training_environment()}
    cuda_initialized = torch.cuda.is_initialized()
    frozen = prepare_training_context(context, profile, tmp_path)
    assert frozen["harness_version"] == 8
    assert frozen["effective_training_config"]["target_modules"] == _config()["target_modules"]
    assert torch.cuda.is_initialized() == cuda_initialized
    profile["config"]["target_modules"] = ["model.layers.99.self_attn.q_proj"]
    with pytest.raises(ValueError, match="modules_not_in_model"):
        prepare_training_context(context, profile, tmp_path)


@pytest.mark.parametrize("failure", ["none", "adapter", "revoked", "existing"])
def test_runner_checks_saved_adapter_before_publication(monkeypatch, tmp_path, failure):
    import train

    context = _training_context()
    observation = _observed(_config())
    saved = copy.deepcopy(observation["adapter_config"])
    if failure == "adapter":
        saved["r"] = 4
    (tmp_path / "adapter_config.json").write_text(json.dumps(saved))
    result = {
        "adapter_path": str(tmp_path),
        "artifact_prefix": context["output_prefix"],
        "configuration_observation": observation,
        "metrics": {
            "gpu_model": "test",
            "gpu_count": 1,
            "steps": 2,
            "processed_tokens": 20,
            "peak_vram_bytes": 1024,
        },
    }
    store = Mock()
    store.get_object_body.return_value = json.dumps(context).encode()
    if failure == "existing":
        from botocore.exceptions import ClientError

        store.client.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )
    monkeypatch.setattr(job_runner, "S3Utils", lambda: store)
    monkeypatch.setattr(job_runner, "_training_approval", lambda *_: None)
    monkeypatch.setattr(train, "train", lambda _: result)
    monkeypatch.setenv("VERIFIER_DATABASE_URL", "postgresql://verifier")
    monkeypatch.setenv("HARNESS_JOB_IMAGE", context["training_image_digest"])
    monkeypatch.delenv("HARNESS_TENANT_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    snapshot = {
        **context,
        "state": "approved",
        "algorithm": "sft",
        "compile_manifest_key": context["compile_manifest_ref"],
        "target_tokenizer_digest": context["tokenizer_digest"],
    }
    if failure == "revoked":

        def revoke(_context):
            snapshot["state"] = "revoked"
            return result

        monkeypatch.setattr(train, "train", revoke)
    evaluation = {
        "subject_type": "base",
        "state": "passed",
        "required_trials": 1,
        "metrics": {"total": 1},
        "hard_gates": {"independent_verifier": True, "invalidated_trials": 0, "judge_only": False},
    }
    monkeypatch.setattr(
        job_runner,
        "ReadOnlyServices",
        lambda *_: SimpleNamespace(snapshot=lambda _: snapshot, evaluation=lambda _: evaluation),
    )
    monkeypatch.setattr(
        job_runner,
        "default_verifiers",
        lambda: SimpleNamespace(
            get=lambda *_: SimpleNamespace(handler=lambda *_: SimpleNamespace(status="passed"))
        ),
    )
    monkeypatch.setattr(job_runner, "_artifact_digest", lambda _: ("a" * 64, 50))
    monkeypatch.setattr(job_runner, "_safetensors_scan", lambda _: {"passed": True})
    service = Mock()
    service.create_adapter_candidate.return_value = "adapter"
    monkeypatch.setattr(job_runner, "EvaluationService", lambda _: service)
    if failure != "none":
        error = {
            "adapter": "peft_mismatch",
            "revoked": "snapshot_missing",
            "existing": "already_exists",
        }[failure]
        with pytest.raises(ValueError, match=error):
            job_runner.run(
                "lora_train", "input", sha256(store.get_object_body.return_value), "result", "job"
            )
        store.upload_directory.assert_not_called()
        if failure != "existing":
            store.client.put_object.assert_not_called()
        service.create_adapter_candidate.assert_not_called()
    else:
        job_runner.run(
            "lora_train", "input", sha256(store.get_object_body.return_value), "result", "job"
        )
        store.client.put_object.assert_called_once()
        assert store.client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
        config = service.create_adapter_candidate.call_args.kwargs["config"]
        assert config["lora"] == saved
        assert config["training_cost_receipt"]["sha256"]
        assert config["training_input"]["ref"] == "input"
        assert config["configuration_verification"]["independent_execution_verified"] is False


@pytest.mark.parametrize("tampered", [False, True, "no_update"])
def test_train_consumes_frozen_config_and_never_uploads(monkeypatch, tmp_path, tampered):
    import torch

    import train

    body = b'{"text":"prompt answer","completion":"answer","split":"train"}\n'
    context = _training_context()
    context["dataset_sha256"] = sha256(body)
    context = freeze_training_config(context, _config(), _environment())
    observed = _observed(_config())
    store = Mock()
    store.get_object_body.return_value = body
    monkeypatch.setattr(train, "S3Utils", lambda: store)
    monkeypatch.setattr(train, "validate_training_model", lambda *_: None)
    monkeypatch.setattr(train, "training_environment", _environment)
    monkeypatch.setattr(train.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    seed = Mock()
    monkeypatch.setattr(train, "set_seed", seed)
    monkeypatch.setenv("H5_TRAIN_MAX_STEPS", "999")
    monkeypatch.setenv("H5_TRAIN_MAX_LENGTH", "1")
    rows = [
        {"text": "prompt answer", "completion": "answer", "split": split}
        for split in ("train", "validation")
    ]

    class Dataset:
        def __iter__(self):
            return iter(rows)

        def filter(self, _predicate):
            return self

        def map(self, fn, **_kwargs):
            self.tokenizer = fn
            return self

    monkeypatch.setattr(train, "load_dataset", lambda *_args, **_kw: Dataset())
    monkeypatch.setattr(
        train.AutoTokenizer,
        "from_pretrained",
        lambda *_args, **_kw: SimpleNamespace(eos_token="eos"),
    )
    names = _config()["target_modules"]
    base = SimpleNamespace(
        dtype=torch.float16, named_modules=lambda: [(name, torch.nn.Linear(1, 1)) for name in names]
    )
    loader = Mock(return_value=base)
    monkeypatch.setattr(train.AutoModelForCausalLM, "from_pretrained", loader)

    class Layer:
        pass

    monkeypatch.setattr(train, "LoraLayer", Layer)
    lora = Mock(
        side_effect=lambda **kw: SimpleNamespace(to_dict=lambda: {**kw, "peft_type": "LORA"})
    )
    monkeypatch.setattr(train, "LoraConfig", lora)

    def save(path):
        directory = tmp_path / "adapter"
        directory.mkdir()
        config = copy.deepcopy(observed["adapter_config"])
        if tampered is True:
            config["r"] = 4
        (directory / "adapter_config.json").write_text(json.dumps(config))

    def peft(model, config):
        return SimpleNamespace(
            peft_config={"default": config},
            named_parameters=lambda: [
                (
                    "model.lora_B.default.weight",
                    torch.zeros(1) if tampered == "no_update" else torch.ones(1),
                )
            ],
            save_pretrained=save,
            get_base_model=lambda: SimpleNamespace(
                named_modules=lambda: [(name, Layer()) for name in names]
            ),
        )

    monkeypatch.setattr(train, "get_peft_model", peft)
    monkeypatch.setattr(
        train, "TrainingArguments", lambda **kw: SimpleNamespace(to_dict=lambda: kw)
    )
    trainer = Mock()

    def make_trainer(**kwargs):
        trainer.args = kwargs["args"]
        trainer.state = SimpleNamespace(global_step=50, num_input_tokens_seen=100)
        return trainer

    monkeypatch.setattr(train, "Trainer", make_trainer)
    monkeypatch.setattr(train.torch.cuda, "reset_peak_memory_stats", lambda: None)
    monkeypatch.setattr(train.torch.cuda, "get_device_name", lambda _: "fake GPU")
    monkeypatch.setattr(train.torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(train.torch.cuda, "max_memory_allocated", lambda: 100)
    if tampered == "no_update":
        with pytest.raises(RuntimeError, match="training_no_weight_update"):
            train.train(context)
    elif tampered:
        with pytest.raises(ValueError, match="peft_mismatch"):
            train.train(context)
    else:
        result = train.train(context)
        assert result["configuration_observation"]["trainer_config"]["max_steps"] == 50
        assert result["configuration_observation"]["preprocessing"]["max_length"] == 512
        assert loader.call_args.kwargs["local_files_only"] is True
        assert lora.call_args.kwargs["target_modules"] == names
        trainer.train.assert_called_once()
        seed.assert_called_once_with(42)
    store.upload_directory.assert_not_called()
