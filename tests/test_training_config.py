"""EC4 contract checks; observations here are synthetic, not GPU evidence."""

import copy

import pytest

from harness.training_config import (
    freeze_training_config,
    validate_effective_training_config,
    validate_frozen_training_config,
    verify_training_config_observation,
)


def _config():
    return {
        "schema_version": "effective_training_config.v1",
        "algorithm": "lora",
        "dtype": "float16",
        "bias": "none",
        "task_type": "CAUSAL_LM",
        "r": 16,
        "lora_alpha": 32,
        "lora_dropout": 0.05,
        "target_modules": ["model.layers.0.self_attn.q_proj", "model.layers.1.self_attn.q_proj"],
        "max_length": 512,
        "per_device_train_batch_size": 4,
        "per_device_eval_batch_size": 8,
        "gradient_accumulation_steps": 1,
        "learning_rate": 0.0003,
        "lr_scheduler_type": "cosine",
        "weight_decay": 0.01,
        "warmup_steps": 0,
        "max_steps": 50,
        "eval_steps": 5,
        "save_steps": 5,
        "eval_strategy": "steps",
        "save_strategy": "steps",
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "seed": 42,
        "data_seed": 42,
        "optim": "adamw_torch",
        "adam_beta1": 0.9,
        "adam_beta2": 0.999,
        "adam_epsilon": 1e-8,
        "max_grad_norm": 1.0,
        "completion_only": True,
        "padding": "max_length",
        "truncation_side": "left",
        "add_special_tokens": True,
    }


def _context():
    return {
        "tenant_id": "ec4-synthetic",
        "snapshot_id": "snapshot-1",
        "dataset_sha256": "a" * 64,
        "base_model_digest": "b" * 64,
        "tokenizer_digest": "c" * 64,
        "chat_template_digest": "d" * 64,
        "compile_manifest_sha256": "e" * 64,
    }


def _environment():
    return dict.fromkeys(
        ("python", "torch", "transformers", "peft", "datasets", "accelerate"), "test"
    )


def _observed(config):
    peft = {
        key: config[key]
        for key in ("r", "lora_alpha", "lora_dropout", "bias", "task_type", "target_modules")
    }
    peft["peft_type"] = "LORA"
    return {
        "peft_config": copy.deepcopy(peft),
        "adapter_config": copy.deepcopy(peft),
        "trainer_config": {**config, "fp16": True, "bf16": False},
        "target_modules": list(config["target_modules"]),
        "environment": _environment(),
        "model_dtype": "float16",
        "preprocessing": {
            key: config[key]
            for key in (
                "max_length",
                "completion_only",
                "padding",
                "truncation_side",
                "add_special_tokens",
            )
        },
    }


def test_freeze_is_detached_deterministic_and_not_an_approval(monkeypatch):
    source, config, environment = _context(), _config(), _environment()
    frozen = freeze_training_config(source, config, environment)
    reverse_config = dict(reversed(list(config.items())))
    assert frozen == freeze_training_config(source, reverse_config, environment)
    source["tenant_id"] = "changed"
    config["target_modules"].clear()
    environment["torch"] = "changed"
    monkeypatch.setenv("H5_TRAIN_MAX_STEPS", "999")
    assert validate_frozen_training_config(frozen)["max_steps"] == 50
    assert "snapshot_state" not in frozen
    assert "harness_version" not in frozen  # Does not silently upgrade/approve an old context.
    receipt = verify_training_config_observation(frozen, **_observed(_config()))
    assert receipt["configuration_consistent"] is True
    assert receipt["independent_execution_verified"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("r", True),
        ("max_length", 0),
        ("seed", -1),
        ("warmup_steps", 51),
        ("lora_dropout", float("nan")),
        ("learning_rate", float("inf")),
        ("weight_decay", False),
        ("learning_rate", 0),
        ("lora_dropout", 1),
        ("dtype", "bfloat16"),
        ("algorithm", "qlora"),
        ("save_steps", 6),
        ("eval_steps", 51),
        ("save_steps", 55),
        ("load_best_model_at_end", 1),
        ("target_modules", ["q_proj"]),
        ("target_modules", "all-linear"),
        ("target_modules", ["model.layers.*.q_proj"]),
        ("target_modules", ["model.q_proj", "model.q_proj"]),
        ("target_modules", ["model.z_proj", "model.q_proj"]),
        ("optim", "adamw_torch_fused"),
        ("completion_only", False),
    ],
)
def test_config_rejects_implicit_or_unsupported_values(key, value):
    with pytest.raises(ValueError, match="training_config_"):
        validate_effective_training_config({**_config(), key: value})


def test_config_rejects_missing_unknown_fields_and_invalid_container():
    config = _config()
    del config["seed"]
    for value in (config, {**_config(), "unknown": 1}, None, []):
        with pytest.raises(ValueError, match="fields_invalid"):
            validate_effective_training_config(value)


@pytest.mark.parametrize(
    "key",
    [
        "tenant_id",
        "snapshot_id",
        "dataset_sha256",
        "base_model_digest",
        "tokenizer_digest",
        "chat_template_digest",
        "compile_manifest_sha256",
        "training_binding_sha256",
    ],
)
def test_binding_tamper_is_rejected(key):
    frozen = freeze_training_config(_context(), _config(), _environment())
    frozen[key] = "f" * 64
    with pytest.raises(ValueError, match="binding_mismatch"):
        validate_frozen_training_config(frozen)


def test_config_hash_and_environment_tampering_are_rejected():
    frozen = freeze_training_config(_context(), _config(), _environment())
    frozen["effective_training_config"]["r"] = 8
    with pytest.raises(ValueError, match="hash_mismatch"):
        validate_frozen_training_config(frozen)
    frozen = freeze_training_config(_context(), _config(), _environment())
    frozen["training_environment"]["torch"] = "changed"
    with pytest.raises(ValueError, match="binding_mismatch"):
        validate_frozen_training_config(frozen)
    with pytest.raises(ValueError, match="environment_invalid"):
        freeze_training_config(_context(), _config(), {})
    with pytest.raises(ValueError, match="binding_hash_invalid"):
        freeze_training_config(
            {**_context(), "dataset_sha256": "x" * 64}, _config(), _environment()
        )


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("peft_config", "r", 8),
        ("adapter_config", "lora_alpha", 8),
        ("adapter_config", "target_modules", ["q_proj"]),
        ("adapter_config", "use_dora", True),
        ("peft_config", "use_rslora", True),
        ("adapter_config", "rank_pattern", {"model.q_proj": 8}),
        ("adapter_config", "modules_to_save", ["lm_head"]),
        ("adapter_config", "layer_replication", [[0, 1]]),
        ("peft_config", "fan_in_fan_out", True),
        ("adapter_config", "lora_bias", True),
        ("peft_config", "init_lora_weights", "pissa"),
        ("adapter_config", "layers_to_transform", 0),
        ("trainer_config", "learning_rate", 0.1),
        ("trainer_config", "seed", 10),
        ("trainer_config", "gradient_accumulation_steps", True),
        ("trainer_config", "greater_is_better", 0),
        ("trainer_config", "bf16", True),
        ("trainer_config", "fp16", False),
        ("trainer_config", "optim", "adamw_torch_fused"),
        ("preprocessing", "max_length", 256),
        ("preprocessing", "completion_only", 1),
        ("environment", "torch", "changed"),
    ],
)
def test_runtime_or_saved_adapter_mismatch_is_rejected(section, key, value):
    frozen = freeze_training_config(_context(), _config(), _environment())
    observation = _observed(_config())
    observation[section][key] = value
    with pytest.raises(ValueError, match="training_config_"):
        verify_training_config_observation(frozen, **observation)


@pytest.mark.parametrize(
    "key,value",
    [
        ("target_modules", ["model.layers.0.self_attn.q_proj"]),
        ("target_modules", ["model.layers.0.self_attn.q_proj"] * 2),
        ("model_dtype", "bfloat16"),
    ],
)
def test_actual_module_set_and_model_dtype_are_checked(key, value):
    frozen = freeze_training_config(_context(), _config(), _environment())
    observation = _observed(_config())
    observation[key] = value
    with pytest.raises(ValueError, match="training_config_"):
        verify_training_config_observation(frozen, **observation)
