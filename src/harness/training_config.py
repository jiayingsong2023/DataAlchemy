"""Frozen FP16 LoRA configuration; no model, environment or network defaults."""

from __future__ import annotations

import copy
import math
import os
import platform
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any

from core.evidence import sha256

_FIXED = {
    "schema_version": "effective_training_config.v1",
    "algorithm": "lora",
    "dtype": "float16",
    "bias": "none",
    "task_type": "CAUSAL_LM",
    "lr_scheduler_type": "cosine",
    "eval_strategy": "steps",
    "save_strategy": "steps",
    "load_best_model_at_end": True,
    "metric_for_best_model": "eval_loss",
    "greater_is_better": False,
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
_POSITIVE_INTS = {
    "r",
    "lora_alpha",
    "max_length",
    "per_device_train_batch_size",
    "per_device_eval_batch_size",
    "gradient_accumulation_steps",
    "max_steps",
    "eval_steps",
    "save_steps",
}
_NONNEGATIVE_INTS = {"warmup_steps", "seed", "data_seed"}
_FLOATS = {"lora_dropout", "learning_rate", "weight_decay"}
_BINDING_FIELDS = (
    "tenant_id",
    "snapshot_id",
    "dataset_sha256",
    "base_model_digest",
    "tokenizer_digest",
    "chat_template_digest",
    "compile_manifest_sha256",
    "effective_training_config_sha256",
    "training_environment",
)


def validate_effective_training_config(  # noqa: C901 - keep schema checks auditable together
    value: Any,
) -> dict[str, Any]:
    """Accept the supported algorithm only, with every consequential knob explicit."""
    expected = _FIXED.keys() | _POSITIVE_INTS | _NONNEGATIVE_INTS | _FLOATS | {"target_modules"}
    if not isinstance(value, dict) or value.keys() != expected:
        raise ValueError("training_config_fields_invalid")
    for key, required in _FIXED.items():
        if type(value[key]) is not type(required) or value[key] != required:
            raise ValueError(f"training_config_unsupported:{key}")
    for keys, minimum in ((_POSITIVE_INTS, 1), (_NONNEGATIVE_INTS, 0)):
        for key in keys:
            if type(value[key]) is not int or value[key] < minimum:
                raise ValueError(f"training_config_integer_invalid:{key}")
    for key in _FLOATS:
        number = value[key]
        if (
            type(number) not in {int, float}
            or (type(number) is float and not math.isfinite(number))
            or not 0 <= number <= 1
        ):
            raise ValueError(f"training_config_number_invalid:{key}")
    if value["lora_dropout"] >= 1 or value["learning_rate"] == 0:
        raise ValueError("training_config_number_out_of_range")
    if (
        value["save_steps"] % value["eval_steps"]
        or value["eval_steps"] > value["max_steps"]
        or value["save_steps"] > value["max_steps"]
        or value["warmup_steps"] > value["max_steps"]
    ):
        raise ValueError("training_config_schedule_invalid")
    modules = value["target_modules"]
    if (
        not isinstance(modules, list)
        or not modules
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_0-9]\w*)+", name) is None
            for name in modules
        )
        or modules != sorted(set(modules))
    ):
        raise ValueError("training_config_exact_modules_required")
    return copy.deepcopy(value)


def training_binding(context: dict[str, Any]) -> dict[str, Any]:  # noqa: C901 - explicit binding checks
    """Bind declared data/model inputs, not credentials, paths or approval status."""
    if any(key not in context for key in _BINDING_FIELDS):
        raise ValueError("training_config_binding_incomplete")
    for key in _BINDING_FIELDS:
        if key.endswith(("sha256", "digest")) and (
            not isinstance(context[key], str) or re.fullmatch("[0-9a-f]{64}", context[key]) is None
        ):
            raise ValueError(f"training_config_binding_hash_invalid:{key}")
    if any(
        not isinstance(context[key], str) or not context[key]
        for key in ("tenant_id", "snapshot_id")
    ):
        raise ValueError("training_config_binding_identity_invalid")
    environment = context["training_environment"]
    required = {"python", "torch", "transformers", "peft", "datasets", "accelerate"}
    if (
        not isinstance(environment, dict)
        or environment.keys() != required
        or any(
            not isinstance(version, str) or not version.strip() for version in environment.values()
        )
    ):
        raise ValueError("training_config_environment_invalid")
    binding = {key: context[key] for key in _BINDING_FIELDS}
    if "model_metadata_sha256" in context:
        value = context["model_metadata_sha256"]
        if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
            raise ValueError("training_model_metadata_hash_invalid")
        binding["model_metadata_sha256"] = value
    for key in ("training_code_sha256", "training_image_digest"):
        if key in context:
            value = context[key]
            pattern = r"sha256:[0-9a-f]{64}" if key.endswith("digest") else r"[0-9a-f]{64}"
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise ValueError(f"training_execution_fingerprint_invalid:{key}")
            binding[key] = value
    return copy.deepcopy(binding)


def freeze_training_config(
    context: dict[str, Any], config: dict[str, Any], environment: dict[str, str]
) -> dict[str, Any]:
    """Creation-side sealing only; hashing is NOT training permission or approval."""
    frozen = copy.deepcopy(context)
    frozen["effective_training_config"] = validate_effective_training_config(config)
    frozen["effective_training_config_sha256"] = sha256(frozen["effective_training_config"])
    frozen["training_environment"] = copy.deepcopy(environment)
    frozen["training_binding_sha256"] = sha256(training_binding(frozen))
    return frozen


def validate_frozen_training_config(context: dict[str, Any]) -> dict[str, Any]:
    config = validate_effective_training_config(context.get("effective_training_config"))
    if context.get("effective_training_config_sha256") != sha256(config):
        raise ValueError("training_config_hash_mismatch")
    if context.get("training_binding_sha256") != sha256(training_binding(context)):
        raise ValueError("training_config_binding_mismatch")
    return config


def _matches(actual: Any, expected: Any) -> bool:
    """Python considers True == 1; configuration evidence must not."""
    return isinstance(actual, bool) == isinstance(expected, bool) and actual == expected


def training_environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        **{
            name: version(name)
            for name in ("torch", "transformers", "peft", "datasets", "accelerate")
        },
    }


def training_code_fingerprint() -> str:
    from harness.evaluation import _tree_digest

    return _tree_digest(Path(__file__).resolve().parents[1], ("**/*.py",))


def training_kwargs(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = validate_effective_training_config(config)
    lora_keys = {"r", "lora_alpha", "lora_dropout", "bias", "task_type", "target_modules"}
    excluded = lora_keys | {
        "schema_version",
        "algorithm",
        "dtype",
        "max_length",
        "completion_only",
        "padding",
        "truncation_side",
        "add_special_tokens",
    }
    return (
        {key: config[key] for key in lora_keys},
        {
            **{key: value for key, value in config.items() if key not in excluded},
            "fp16": True,
            "bf16": False,
        },
    )


def validate_training_model(context: dict[str, Any], model_dir: str | Path) -> None:
    from harness.evaluation import _tree_digest, model_path_fingerprint

    path = Path(model_dir).resolve()
    actual = model_path_fingerprint(path, model_root=path)
    for source, target in (
        ("model_sha256", "base_model_digest"),
        ("tokenizer_sha256", "tokenizer_digest"),
        ("chat_template_sha256", "chat_template_digest"),
    ):
        if actual[source] != context[target]:
            raise ValueError(f"training_model_content_mismatch:{target}")
    if _tree_digest(path, ("*.json", "*.jinja")) != context["model_metadata_sha256"]:
        raise ValueError("training_model_metadata_mismatch")


def prepare_training_context(
    context: dict[str, Any],
    profile: dict[str, Any],
    model_dir: str | Path,
) -> dict[str, Any]:
    """Creation-side validation of an explicit profile; never authorizes execution."""
    from harness.evaluation import _tree_digest
    from harness.jobs import validate_training_context

    if not isinstance(profile, dict) or profile.keys() != {"config", "environment"}:
        raise ValueError("training_profile_invalid")
    frozen = freeze_training_config(context, profile["config"], profile["environment"])
    frozen["harness_version"] = 8
    frozen["model_metadata_sha256"] = _tree_digest(Path(model_dir), ("*.json", "*.jinja"))
    frozen["training_code_sha256"] = training_code_fingerprint()
    image = os.environ.get("HARNESS_JOB_IMAGE", "")
    frozen["training_image_digest"] = image.rsplit("@", 1)[-1]
    # Bind loading metadata separately as historical model fingerprints cover weights only.
    frozen["training_binding_sha256"] = sha256(training_binding(frozen))
    validate_training_model(frozen, model_dir)
    # No weights, network access or GPU allocation needed to resolve the named linear layers.
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM

    with torch.device("meta"):
        model = AutoModelForCausalLM.from_config(
            AutoConfig.from_pretrained(str(model_dir), local_files_only=True),
            trust_remote_code=False,
        )
    available = {
        name for name, module in model.named_modules() if isinstance(module, torch.nn.Linear)
    }
    if not set(frozen["effective_training_config"]["target_modules"]) <= available:
        raise ValueError("training_config_modules_not_in_model")
    return validate_training_context(frozen, for_execution=True)


def verify_training_config_observation(  # noqa: C901 - linear independent comparisons
    context: dict[str, Any],
    *,
    peft_config: dict[str, Any],
    trainer_config: dict[str, Any],
    adapter_config: dict[str, Any],
    target_modules: list[str],
    environment: dict[str, str],
    preprocessing: dict[str, Any],
    model_dtype: str,
) -> dict[str, Any]:
    """Compare observed runtime/artifact values; never project them from the request."""
    config = validate_frozen_training_config(context)
    if (
        any(
            not isinstance(value, dict)
            for value in (peft_config, trainer_config, adapter_config, environment, preprocessing)
        )
        or not isinstance(target_modules, list)
        or any(not isinstance(name, str) for name in target_modules)
    ):
        raise ValueError("training_config_observation_invalid")
    if environment != context["training_environment"]:
        raise ValueError("training_config_environment_mismatch")
    if sorted(target_modules) != config["target_modules"]:
        raise ValueError("training_config_resolved_modules_mismatch")
    preprocessing_fields = {
        "max_length",
        "completion_only",
        "padding",
        "truncation_side",
        "add_special_tokens",
    }
    if preprocessing.keys() != preprocessing_fields or any(
        not _matches(preprocessing[key], config[key]) for key in preprocessing_fields
    ):
        raise ValueError("training_config_preprocessing_mismatch")
    if model_dtype != config["dtype"]:
        raise ValueError("training_config_model_dtype_mismatch")
    lora_fields = {"r", "lora_alpha", "lora_dropout", "bias", "task_type"}
    for observed in (peft_config, adapter_config):
        if any(not _matches(observed.get(key), config[key]) for key in lora_fields):
            raise ValueError("training_config_peft_mismatch")
        modules = observed.get("target_modules")
        if (
            not isinstance(modules, (list, set))
            or any(not isinstance(name, str) for name in modules)
            or sorted(modules) != config["target_modules"]
        ):
            raise ValueError("training_config_peft_modules_mismatch")
        # Standard LoRA only. A matching rank does not make DoRA/RSLoRA equivalent.
        if (
            observed.get("peft_type") != "LORA"
            or observed.get("use_dora", False) is not False
            or observed.get("use_rslora", False) is not False
            or observed.get("rank_pattern", {})
            or observed.get("alpha_pattern", {})
            or observed.get("modules_to_save")
            or observed.get("layer_replication")
            or observed.get("fan_in_fan_out", False) is not False
            or observed.get("lora_bias", False) is not False
            or observed.get("use_qalora", False) is not False
            or observed.get("init_lora_weights", True) is not True
            or observed.get("trainable_token_indices")
            or observed.get("target_parameters")
            or observed.get("exclude_modules")
            or observed.get("layers_to_transform") is not None
            or observed.get("layers_pattern") is not None
        ):
            raise ValueError("training_config_peft_variant_mismatch")
    trainer_fields = (
        (_POSITIVE_INTS | _NONNEGATIVE_INTS | _FLOATS | _FIXED.keys())
        - lora_fields
        - preprocessing_fields
        - {"schema_version", "algorithm", "dtype"}
    )
    if any(not _matches(trainer_config.get(key), config[key]) for key in trainer_fields):
        raise ValueError("training_config_trainer_mismatch")
    if trainer_config.get("fp16") is not True or trainer_config.get("bf16") is not False:
        raise ValueError("training_config_dtype_mismatch")
    return {
        "effective_training_config_sha256": context["effective_training_config_sha256"],
        "training_binding_sha256": context["training_binding_sha256"],
        "configuration_consistent": True,
        "independent_execution_verified": False,
    }
