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
    def __init__(self, *args, **kwargs): pass

class TensorSubmoduleHook:
    """Intercepts torch.distributed.tensor and all submodules"""
    def find_spec(self, name, path, target=None):
        if name == 'torch.distributed.tensor' or name.startswith('torch.distributed.tensor.'):
            class Loader:
                def create_module(self_loader, spec):
                    module = types.ModuleType(spec.name)
                    module.__path__ = []
                    if spec.name == 'torch.distributed.tensor':
                        module.DTensor = DummyDTensor
                        module.Shard = DummyPlacement
                        module.Replicate = DummyPlacement
                        module.Partial = DummyPlacement
                    elif '_dtensor_spec' in spec.name:
                        module.DTensorSpec = type('DTensorSpec', (), {})
                        module.TensorMeta = type('TensorMeta', (), {})
                    elif 'placement_types' in spec.name:
                        module.Placement = DummyPlacement
                        module.Shard = DummyPlacement
                        module.Replicate = DummyPlacement
                        module.Partial = DummyPlacement
                        module._StridedShard = DummyPlacement
                    elif 'device_mesh' in spec.name:
                        module._mesh_resources = type('_mesh_resources', (), {})
                        module.DeviceMesh = type('DeviceMesh', (), {})
                    return module
                def exec_module(self_loader, module):
                    pass
            return ModuleSpec(name, Loader())
        return None

if not any(isinstance(hook, TensorSubmoduleHook) for hook in sys.meta_path):
    sys.meta_path.insert(0, TensorSubmoduleHook())

import os

import torch
import torch.distributed
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainingArguments,
)

from config import (
    ADAPTER_S3_PREFIX,
    S3_ACCESS_KEY,
    S3_ENDPOINT,
    S3_SECRET_KEY,
    SFT_OUTPUT_PATH,
    SFT_S3_PATH,
    get_model_config,
)
from utils.s3_utils import S3Utils


def train():
    # Load Model C config
    model_c = get_model_config("model_c")
    # Priority: model_path > model_id
    model_id = model_c.get("model_path") or model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")
    lora_config = model_c.get("lora", {})

    # 0. Check Dataset first (S3 preferred)
    s3 = S3Utils()
    # If using local model path, we should ensure tokenizer and model use local_files_only if possible
    is_local_model = os.path.exists(model_id) if model_id else False
    if is_local_model:
        print(f"[*] Using local base model from: {model_id}")

    s3_key = SFT_S3_PATH.replace(f"s3://{s3.bucket}/", "")
    if not s3.exists(s3_key):
        print(f"[!] SFT data not found in S3: {SFT_S3_PATH}. Checking local...")
        if not os.path.exists(SFT_OUTPUT_PATH):
            raise FileNotFoundError(f"Unable to find SFT data in S3 or local: {SFT_OUTPUT_PATH}")
        dataset_path = SFT_OUTPUT_PATH
        streaming = False
        storage_options = None
    else:
        print(f"[*] Found SFT data in S3: {SFT_S3_PATH}. Enabling Streaming Mode.")
        dataset_path = SFT_S3_PATH
        streaming = True
        storage_options = {
            "key": S3_ACCESS_KEY,
            "secret": S3_SECRET_KEY,
            "client_kwargs": {
                "endpoint_url": S3_ENDPOINT
            }
        }

    # 1. Load Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=is_local_model)
    tokenizer.pad_token = tokenizer.eos_token

    try:
        # 2. Load Model
        # ...
        print(f"Loading model {model_id}...")
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            dtype=torch.float16,
            device_map="auto",
            local_files_only=is_local_model
        )

        # 3. Prepare for LoRA
        # ...
        config = LoraConfig(
            r=lora_config.get("r", 16),
            lora_alpha=lora_config.get("alpha", 32),
            target_modules=lora_config.get("target_modules", ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]),
            lora_dropout=lora_config.get("dropout", 0.05),
            bias="none",
            task_type="CAUSAL_LM"
        )

        model = get_peft_model(model, config)
        model.print_trainable_parameters()

        # 4. Load Dataset (Streaming from S3)
        print(f"Loading dataset from {dataset_path} (streaming={streaming})...")
        dataset = load_dataset(
            "json",
            data_files=dataset_path,
            split="train",
            streaming=streaming,
            storage_options=storage_options
        )

        def tokenize_function(examples):
            return tokenizer(examples["text"], truncation=True, padding="max_length", max_length=512)

        # Note: In streaming mode, map() behaves slightly differently but still works for tokenization
        tokenized_dataset = dataset.map(tokenize_function, batched=True, remove_columns=["text"])

        # 5. Training Arguments
        # ... (same as before)
        training_args = TrainingArguments(
            output_dir="./lora-tiny-llama",
            per_device_train_batch_size=4,
            gradient_accumulation_steps=1,  # 立即更新权重，适合小数据集
            learning_rate=3e-4,
            lr_scheduler_type="cosine",
            warmup_steps=5,                 # 调小预热步数
            weight_decay=0.01,
            logging_steps=1,                # 每一步都打印日志
            max_steps=50,                   # 50步足够测试
            save_steps=50,
            fp16=True,
            push_to_hub=False,
            report_to="none"
        )

        # 6. Trainer
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_dataset,
            data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
        )

        print("Starting training...")
        trainer.train()

        # 7. Save Adapter
        local_adapter_path = "./lora-tiny-llama-adapter"
        model.save_pretrained(local_adapter_path)
        print(f"Training complete. Adapter saved to {local_adapter_path}")

        # 8. Upload to S3
        print(f"[*] Uploading adapter to S3: {ADAPTER_S3_PREFIX}...")
        if s3.upload_directory(local_adapter_path, ADAPTER_S3_PREFIX):
            print("[SUCCESS] Adapter synced to S3.")
        else:
            print("[!] Failed to sync adapter to S3.")

    except Exception as e:
        print(f"Error during training: {e}")
        raise e
    finally:
        # Cleanup to prevent hangs on ROCm Windows
        if 'model' in locals():
            del model
        if 'trainer' in locals():
            del trainer
        torch.cuda.empty_cache()
        import gc
        gc.collect()
        print("[System] GPU resources released.")

if __name__ == "__main__":
    try:
        train()
    except Exception as e:
        print(f"FATAL: {e}")
        os._exit(1)

    print("✅ Training process finished successfully.")
    sys.stdout.flush()
    os._exit(0)
