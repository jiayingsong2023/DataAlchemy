"""
Model Manager with torch.compile optimization for AMD AI Max+ 395
Singleton pattern to ensure only one model instance is loaded
"""
import os
import torch
from typing import Optional, Dict, Any
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
import threading


class ModelManager:
    """Singleton model manager with lazy loading and optimization"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.base_model = None
        self.lora_model = None
        self.tokenizer = None
        self.device = None
        self._initialized = True
        
        print("[ModelManager] Initialized (models not loaded yet)")
    
    def load_models(self, base_model_id: str, lora_adapter_path: Optional[str] = None, 
                   device: str = "auto", compile_model: bool = True):
        """
        Load models with optimization for AMD GPU
        
        Args:
            base_model_id: HuggingFace model ID or local path
            lora_adapter_path: Path to LoRA adapter (optional)
            device: Device to use ('auto', 'cuda', 'cpu')
            compile_model: Whether to use torch.compile (PyTorch 2.0+)
        """
        if self.base_model is not None:
            print("[ModelManager] Models already loaded, skipping...")
            return
        
        print(f"[ModelManager] Loading base model: {base_model_id}")
        
        # Determine device
        if device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device
        
        print(f"[ModelManager] Using device: {self.device}")
        
        # Load tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(base_model_id)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        
        # Load base model with optimizations
        print("[ModelManager] Loading base model...")
        self.base_model = AutoModelForCausalLM.from_pretrained(
            base_model_id,
            torch_dtype=torch.float16,  # Use FP16 for AMD GPU
            device_map=self.device,
            low_cpu_mem_usage=True
        )
        
        # Load LoRA adapter if provided
        if lora_adapter_path and os.path.exists(lora_adapter_path):
            print(f"[ModelManager] Loading LoRA adapter: {lora_adapter_path}")
            try:
                self.lora_model = PeftModel.from_pretrained(
                    self.base_model,
                    lora_adapter_path,
                    torch_dtype=torch.float16
                )
                self.lora_model.eval()
                model_to_optimize = self.lora_model
                print("[ModelManager] LoRA adapter loaded successfully")
            except Exception as e:
                print(f"[ModelManager] Warning: Failed to load LoRA adapter: {e}")
                print("[ModelManager] Continuing with base model only...")
                self.base_model.eval()
                model_to_optimize = self.base_model
        else:
            self.base_model.eval()
            model_to_optimize = self.base_model
        
        # Apply torch.compile optimization (PyTorch 2.0+)
        if compile_model and hasattr(torch, 'compile'):
            try:
                print("[ModelManager] Applying torch.compile optimization...")
                # Use 'reduce-overhead' mode for better latency
                # 'max-autotune' can be used for throughput but takes longer to compile
                compiled_model = torch.compile(
                    model_to_optimize,
                    mode="reduce-overhead",
                    backend="inductor"
                )
                
                if lora_adapter_path:
                    self.lora_model = compiled_model
                else:
                    self.base_model = compiled_model
                    
                print("[ModelManager] torch.compile applied successfully")
            except Exception as e:
                print(f"[ModelManager] Warning: torch.compile failed: {e}")
                print("[ModelManager] Continuing without compilation...")
        
        # Warmup: run a dummy forward pass to compile kernels
        print("[ModelManager] Warming up model...")
        self._warmup()
        
        print("[ModelManager] Model loading complete!")
    
    def _warmup(self):
        """Run dummy inference to compile kernels and warm up cache"""
        try:
            dummy_input = self.tokenizer("Hello", return_tensors="pt").to(self.device)
            with torch.no_grad():
                with torch.autocast(device_type='cuda', dtype=torch.float16):
                    model = self.lora_model if self.lora_model else self.base_model
                    _ = model.generate(**dummy_input, max_new_tokens=10)
            print("[ModelManager] Warmup complete")
        except Exception as e:
            print(f"[ModelManager] Warmup failed: {e}")
    
    def generate(self, prompts: list[str], generation_kwargs: dict = None) -> list[str]:
        """
        Generate text for a batch of prompts with AMD GPU optimizations
        
        Args:
            prompts: List of input prompts
            generation_kwargs: Dictionary of generation parameters
        
        Returns:
            List of generated texts
        """
        if self.base_model is None:
            raise RuntimeError("Models not loaded. Call load_models() first.")
        
        if generation_kwargs is None:
            generation_kwargs = {}
        
        # Tokenize batch
        inputs = self.tokenizer(
            prompts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512
        ).to(self.device)
        
        # Default generation parameters optimized for AMD
        default_kwargs = {
            "max_new_tokens": 256,
            "do_sample": True,
            "temperature": 0.7,
            "top_p": 0.9,
            "pad_token_id": self.tokenizer.pad_token_id,
            "eos_token_id": self.tokenizer.eos_token_id,
        }
        default_kwargs.update(generation_kwargs)
        
        # Generate with mixed precision
        model = self.lora_model if self.lora_model else self.base_model
        
        with torch.no_grad():
            with torch.autocast(device_type='cuda', dtype=torch.float16):
                outputs = model.generate(**inputs, **default_kwargs)
        
        # Decode outputs
        generated_texts = self.tokenizer.batch_decode(
            outputs,
            skip_special_tokens=True
        )
        
        return generated_texts
    
    def clear_cache(self):
        """Clear GPU cache to free memory"""
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            print("[ModelManager] GPU cache cleared")
    
    def get_memory_usage(self) -> Dict[str, Any]:
        """Get current GPU memory usage"""
        if not torch.cuda.is_available():
            return {"error": "CUDA not available"}
        
        return {
            "allocated_gb": torch.cuda.memory_allocated() / 1e9,
            "reserved_gb": torch.cuda.memory_reserved() / 1e9,
            "max_allocated_gb": torch.cuda.max_memory_allocated() / 1e9,
        }
