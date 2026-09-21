import sys
import types
from importlib.machinery import ModuleSpec

# Fix for ROCm's PyTorch 2.9.1 circular import bug in torch.distributed.tensor
# This hook intercepts the broken module and provides dummy implementations


class DummyDTensor:
    """Dummy DTensor class for isinstance() checks"""

    pass


class DummyPlacement:
    """Dummy placement class that accepts arguments"""

    def __init__(self, *args, **kwargs):
        pass


class TensorSubmoduleHook:
    """Intercepts torch.distributed.tensor and all submodules"""

    def find_spec(self, name, path, target=None):
        if name == "torch.distributed.tensor" or name.startswith("torch.distributed.tensor."):

            class Loader:
                def create_module(self_loader, spec):
                    module = types.ModuleType(spec.name)
                    module.__path__ = []
                    if spec.name == "torch.distributed.tensor":
                        module.DTensor = DummyDTensor
                        module.Shard = DummyPlacement
                        module.Replicate = DummyPlacement
                        module.Partial = DummyPlacement
                    elif "_dtensor_spec" in spec.name:
                        module.DTensorSpec = type("DTensorSpec", (), {})
                        module.TensorMeta = type("TensorMeta", (), {})
                    elif "placement_types" in spec.name:
                        module.Placement = DummyPlacement
                        module.Shard = DummyPlacement
                        module.Replicate = DummyPlacement
                        module.Partial = DummyPlacement
                        module._StridedShard = DummyPlacement
                    elif "device_mesh" in spec.name:
                        module._mesh_resources = type("_mesh_resources", (), {})
                        module.DeviceMesh = type("DeviceMesh", (), {})
                    return module

                def exec_module(self_loader, module):
                    pass

            return ModuleSpec(name, Loader())
        return None


if not any(isinstance(hook, TensorSubmoduleHook) for hook in sys.meta_path):
    sys.meta_path.insert(0, TensorSubmoduleHook())

import json
import os
import tempfile
from pathlib import Path

import torch
import torch.distributed
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from peft.tuners.lora.layer import LoraLayer
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    default_data_collator,
    set_seed,
)

from core.evidence import sha256
from harness.jobs import validate_training_context
from harness.training_config import (
    training_environment,
    training_kwargs,
    validate_training_model,
    verify_training_config_observation,
)
from utils.s3_utils import S3Utils


def _completion_only_batch(tokenizer, texts, completions, max_length):
    """Tokenize from the right and mask everything before the reviewed completion."""
    rows = []
    for text, completion in zip(texts, completions, strict=True):
        character_start = text.rfind(completion)
        if character_start < 0:
            raise ValueError("h7_completion_boundary_missing")
        encoded = tokenizer(text, add_special_tokens=True, return_offsets_mapping=True)
        full = encoded["input_ids"]
        start = next(
            (
                index
                for index, (_begin, end) in enumerate(encoded["offset_mapping"])
                if end > character_start
            ),
            None,
        )
        if start is None:
            raise ValueError("h7_completion_boundary_missing")
        offset = max(0, len(full) - max_length)
        full = full[offset:]
        start -= offset
        if start < 0:
            raise ValueError("h7_completion_truncated")
        padding = max_length - len(full)
        rows.append(
            {
                "input_ids": full + [tokenizer.pad_token_id] * padding,
                "attention_mask": [1] * len(full) + [0] * padding,
                "labels": [-100] * start + full[start:] + [-100] * padding,
            }
        )
    return {key: [row[key] for row in rows] for key in rows[0]}


def _exact_lora_modules(model, expected):
    actual = sorted(
        name
        for name, module in model.get_base_model().named_modules()
        if isinstance(module, LoraLayer)
    )
    if actual != expected:
        raise ValueError("training_config_resolved_modules_mismatch")
    # PEFT 0.18 minimizes lists >=20 into suffixes during injection. Canonicalize
    # only after inspecting the injected layers, so saved adapters retain exact scope.
    model.peft_config["default"].target_modules = set(actual)
    return actual


def train(training_context=None):
    if training_context is None:
        raw_context = os.getenv("H5_TRAINING_CONTEXT")
        if not raw_context:
            raise RuntimeError("H5 training requires an approved serialized context")
        training_context = json.loads(raw_context)
    training_context = validate_training_context(training_context, for_execution=True)
    effective = training_context["effective_training_config"]
    environment = training_environment()
    if environment != training_context["training_environment"]:
        raise ValueError("training_config_environment_mismatch")
    model_id = training_context["model_id"]
    validate_training_model(training_context, model_id)
    s3 = S3Utils()
    # Read once: the bytes checked here are the only dataset bytes used for training.
    body = s3.get_object_body(training_context["dataset_key"])
    if body is None or sha256(body) != training_context["dataset_sha256"]:
        raise ValueError("training_dataset_content_mismatch")
    # Job-owned temporary outputs avoid mixing checkpoints/adapters from earlier runs.
    work = Path(tempfile.mkdtemp(prefix="dataalchemy-training-"))
    dataset_path = work / "dataset.jsonl"
    dataset_path.write_bytes(body)
    del body
    dataset = load_dataset("json", data_files=str(dataset_path), split="train", streaming=True)
    columns = list(next(iter(dataset)).keys())
    if not {"text", "completion", "split"} <= set(columns):
        raise ValueError("training_compiled_dataset_required")
    splits = {row.get("split") for row in dataset}
    if splits != {"train", "validation"}:
        raise ValueError("training_dataset_splits_invalid")
    set_seed(effective["seed"])
    tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    tokenizer.pad_token = tokenizer.eos_token
    lora_kwargs, trainer_kwargs = training_kwargs(effective)
    try:
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            dtype=getattr(torch, effective["dtype"]),
            device_map="auto",
            local_files_only=True,
            use_safetensors=True,
        )
        model_dtype = str(model.dtype).removeprefix("torch.")
        available = {
            name for name, module in model.named_modules() if isinstance(module, torch.nn.Linear)
        }
        if not set(effective["target_modules"]) <= available:
            raise ValueError("training_config_modules_not_in_model")
        model = get_peft_model(model, LoraConfig(**lora_kwargs))
        # PEFT input names alone are insufficient; inspect the actual injected modules.
        target_modules = _exact_lora_modules(model, effective["target_modules"])
        preprocessing = {
            "max_length": effective["max_length"],
            "completion_only": True,
            "padding": "max_length",
            "truncation_side": "left",
            "add_special_tokens": True,
        }

        def tokenize_function(examples):
            return _completion_only_batch(
                tokenizer, examples["text"], examples["completion"], preprocessing["max_length"]
            )

        train_dataset = dataset.filter(lambda row: row["split"] == "train").map(
            tokenize_function, batched=True, remove_columns=columns
        )
        validation_dataset = dataset.filter(lambda row: row["split"] == "validation").map(
            tokenize_function, batched=True, remove_columns=columns
        )
        training_args = TrainingArguments(
            output_dir=str(work / "checkpoints"),
            **trainer_kwargs,
            logging_steps=1,
            include_num_input_tokens_seen=True,
            push_to_hub=False,
            report_to="none",
        )
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=validation_dataset,
            data_collator=default_data_collator,
        )
        peft_observation = model.peft_config["default"].to_dict()
        # Persist JSON-native actual values (PEFT uses sets and string enums).
        peft_observation = json.loads(
            json.dumps(
                peft_observation,
                default=lambda value: sorted(value) if isinstance(value, set) else value.value,
            )
        )
        observation = {
            "peft_config": peft_observation,
            "trainer_config": trainer.args.to_dict(),
            "adapter_config": peft_observation,
            "target_modules": target_modules,
            "environment": environment,
            "preprocessing": preprocessing,
            "model_dtype": model_dtype,
        }
        verify_training_config_observation(training_context, **observation)
        torch.cuda.reset_peak_memory_stats()
        trainer.train()
        # Standard LoRA initializes B to zero; global_step also counts AMP-skipped updates.
        if not any(
            bool(parameter.count_nonzero())
            for name, parameter in model.named_parameters()
            if ".lora_B." in name
        ):
            raise RuntimeError("training_no_weight_update")
        local_adapter_path = work / "adapter"
        model.save_pretrained(str(local_adapter_path))
        readme = local_adapter_path / "README.md"
        if readme.is_file():
            readme.unlink()
        observation["adapter_config"] = json.loads(
            (local_adapter_path / "adapter_config.json").read_text()
        )
        observation["trainer_config"] = trainer.args.to_dict()
        verification = verify_training_config_observation(training_context, **observation)
        # Detect source mutation during execution before publishing an adapter.
        validate_training_model(training_context, model_id)
        metrics = {
            "gpu_model": torch.cuda.get_device_name(0),
            "gpu_count": torch.cuda.device_count(),
            "steps": trainer.state.global_step,
            "processed_tokens": int(getattr(trainer.state, "num_input_tokens_seen", 0)),
            "peak_vram_bytes": torch.cuda.max_memory_allocated(),
        }
        if min(metrics["steps"], metrics["processed_tokens"], metrics["peak_vram_bytes"]) < 1:
            raise RuntimeError("h7_training_metrics_missing")
        # Publication belongs to job_runner, after its safety/configuration checks.
        return {
            "adapter_path": str(local_adapter_path),
            "artifact_prefix": training_context["output_prefix"],
            "metrics": metrics,
            "configuration_observation": observation,
            "configuration_verification": verification,
        }
    finally:
        if "trainer" in locals():
            del trainer
        if "model" in locals():
            del model
        torch.cuda.empty_cache()


if __name__ == "__main__":
    try:
        train()
    except Exception as e:
        print(f"FATAL: {e}")
        os._exit(1)

    print("✅ Training process finished successfully.")
    sys.stdout.flush()
    os._exit(0)
