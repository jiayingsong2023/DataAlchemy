"""
Interactive Inference Script for LoRA Fine-tuned Model
Supports interactive chat mode with the fine-tuned model.
"""
import sys
import torch
import torch.distributed

# Monkeypatch for ROCm Windows compatibility with peft
if not hasattr(torch.distributed, "tensor"):
    class Dummy:
        pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel


def load_model(model_id: str, adapter_path: str):
    """Load base model with LoRA adapter."""
    print("Loading base model and tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        device_map="auto",
    )
    
    print("Loading LoRA adapter...")
    model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()
    
    return model, tokenizer


def generate_response(model, tokenizer, user_input: str, max_new_tokens: int = 256) -> str:
    """Generate model response for user input."""
    # Format prompt in instruction format
    prompt = f"### Instruction:\n{user_input}\n\n### Response:\n"
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    inputs = tokenizer(prompt, return_tensors="pt").to(device)
    
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=0.7,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id,
        )
    
    # Decode and extract only the response part
    full_response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    
    # Try to extract just the response after "### Response:"
    if "### Response:" in full_response:
        response = full_response.split("### Response:")[-1].strip()
    else:
        response = full_response[len(prompt):].strip()
    
    return response


def interactive_mode(model, tokenizer):
    """Run interactive chat loop."""
    print("\n" + "=" * 60)
    print("  Interactive Inference Mode")
    print("=" * 60)
    print("  Type your question and press Enter.")
    print("  Commands:")
    print("    quit / exit / q  - Exit the program")
    print("    clear            - Clear screen")
    print("=" * 60 + "\n")
    
    while True:
        try:
            # Get user input
            user_input = input("\n🧑 You: ").strip()
            
            # Check for exit commands
            if user_input.lower() in ["quit", "exit", "q", "bye"]:
                print("\n👋 Goodbye!")
                break
            
            # Check for clear command
            if user_input.lower() == "clear":
                print("\033[H\033[J", end="")  # ANSI clear screen
                continue
            
            # Skip empty input
            if not user_input:
                print("  (Please enter a question)")
                continue
            
            # Generate and display response
            print("\n🤖 Model: ", end="", flush=True)
            response = generate_response(model, tokenizer, user_input)
            print(response)
            
        except KeyboardInterrupt:
            print("\n\n👋 Interrupted. Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            continue


def main():
    model_id = "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T"
    adapter_path = "./lora-tiny-llama-adapter"
    
    print("=" * 60)
    print("  LoRA Fine-tuned Model Inference")
    print("=" * 60)
    
    # Check if adapter exists
    import os
    if not os.path.exists(adapter_path):
        print(f"\n❌ Error: Adapter not found at '{adapter_path}'")
        print("   Please run 'uv run python train.py' first to train the model.")
        sys.exit(1)
    
    # Load model
    model, tokenizer = load_model(model_id, adapter_path)
    print("✅ Model loaded successfully!\n")
    
    # Run interactive mode
    interactive_mode(model, tokenizer)
    
    # Cleanup to prevent hangs on ROCm Windows
    del model
    torch.cuda.empty_cache()
    sys.exit(0)


if __name__ == "__main__":
    main()
