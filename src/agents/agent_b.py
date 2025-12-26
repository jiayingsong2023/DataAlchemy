import torch
import torch.distributed
import sys
import os
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
from config import get_model_config

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "tensor"):
    class Dummy: pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

class AgentB:
    """Agent B: The Model Specialist (LoRA)."""
    
    def __init__(self, model_id: str = None, adapter_path: str = None):
        model_c = get_model_config("model_c")
        self.model_id = model_id or model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")
        self.adapter_path = adapter_path or model_c.get("adapter_path", "./lora-tiny-llama-adapter")
        self.model = None
        self.tokenizer = None

    def _load_model(self):
        if self.model is None:
            print(f"[Agent B] Loading model {self.model_id} and adapter {self.adapter_path}...")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
            base_model = AutoModelForCausalLM.from_pretrained(
                self.model_id,
                dtype=torch.float16,
                device_map="auto",
            )
            if os.path.exists(self.adapter_path):
                self.model = PeftModel.from_pretrained(base_model, self.adapter_path)
            else:
                print(f"[Agent B] WARN: Adapter not found at {self.adapter_path}. Using base model.")
                self.model = base_model
            self.model.eval()

    def predict(self, user_query: str, max_new_tokens: int = 128) -> str:
        """Get 'intuition' from the fine-tuned model."""
        self._load_model()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        prompt = f"### Instruction:\n{user_query}\n\n### Response:\n"
        inputs = self.tokenizer(prompt, return_tensors="pt").to(device)
        
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=0.7,
                do_sample=True,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        
        full_response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        if "### Response:" in full_response:
            return full_response.split("### Response:")[-1].strip()
        return full_response[len(prompt):].strip()

