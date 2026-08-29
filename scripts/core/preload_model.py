#!/usr/bin/env python3
"""
预下载训练所需的基础模型
在训练前运行此脚本可以预先下载模型，避免训练时等待下载
"""

import os
import sys
import types
from importlib.machinery import ModuleSpec

# Fix for ROCm's PyTorch 2.9.1 circular import bug in torch.distributed.tensor
# Same fix as in train.py


class DummyDTensor:
    pass


class DummyPlacement:
    def __init__(self, *args, **kwargs):
        pass


class TensorSubmoduleHook:
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

import torch
import torch.distributed

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from transformers import AutoModelForCausalLM, AutoTokenizer

from config import get_model_config


def preload_model():
    """预下载模型和tokenizer"""
    print("=" * 60)
    print("预下载 LoRA 训练基础模型")
    print("=" * 60)

    # 获取模型配置
    model_c = get_model_config("model_c")
    model_id = model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")

    print(f"\n模型ID: {model_id}")
    print("缓存目录: ~/.cache/huggingface/hub/\n")

    # 1. 下载 Tokenizer
    print("📥 正在下载 Tokenizer...")
    try:
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        print("✅ Tokenizer 下载完成")
        print(f"   - Vocab size: {len(tokenizer)}")
    except Exception as e:
        print(f"❌ Tokenizer 下载失败: {e}")
        return False

    # 2. 下载 Model
    print("\n📥 正在下载模型文件...")
    try:
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            dtype=torch.float16,
            device_map="cpu",
            low_cpu_mem_usage=True,
        )
        print("✅ 模型文件下载完成")
        print(f"   - 模型类型: {type(model).__name__}")
        print(f"   - 参数量: {sum(p.numel() for p in model.parameters()):,}")

        del model
        torch.cuda.empty_cache() if torch.cuda.is_available() else None
        import gc

        gc.collect()

    except Exception as e:
        print(f"❌ 模型下载失败: {e}")
        return False

    print("\n" + "=" * 60)
    print("✅ 预下载完成！模型已缓存到本地")
    print("   现在可以运行 'uv run train-lora' 进行训练")
    print("=" * 60)
    return True


if __name__ == "__main__":
    try:
        success = preload_model()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断下载")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
