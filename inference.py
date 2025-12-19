import sys
import torch
import torch.distributed

# Monkeypatch for ROCm Windows compatibility with peft
if not hasattr(torch.distributed, "tensor"):
    class Dummy:
        pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

def inference():
    model_id = "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T"
    adapter_path = "./lora-tiny-llama-adapter"
    
    print("Loading base model and tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        dtype=torch.float16,
        device_map="auto",
    )
    
    print("Loading LoRA adapter...")
    model = PeftModel.from_pretrained(model, adapter_path)
    
    # Test prompt
    prompt = "### Instruction:\nIn the context of AI fine-tuning, explain what LoRA is in one sentence.\n\n### Input:\n\n### Response:\n"
    
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda" if torch.cuda.is_available() else "cpu")
    
    print("Generating response...")
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=50, temperature=0.7, do_sample=True)
    
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    print("-" * 30)
    print(response)
    print("-" * 30)
    
    # Cleanup to prevent hangs on ROCm Windows
    del model
    torch.cuda.empty_cache()
    sys.exit(0)

if __name__ == "__main__":
    inference()
