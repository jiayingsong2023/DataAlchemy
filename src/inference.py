"""
Interactive Inference Script for LoRA Fine-tuned Model
Supports interactive chat mode with the fine-tuned model.
"""
import sys
import torch
from agents.coordinator import Coordinator

def interactive_mode(coordinator):
    """Run interactive chat loop using Multi-Agent Coordinator."""
    print("\n" + "=" * 60)
    print("  Multi-Agent RAG + LoRA System")
    print("=" * 60)
    print("  Roles:")
    print("    - Agent C: Knowledge Retrieval (RAG)")
    print("    - Agent B: Domain Intuition (LoRA)")
    print("    - Agent D: Final Answer Synthesis (DeepSeek)")
    print("=" * 60)
    print("  Type 'quit' to exit, 'clear' to clear screen.")
    print("=" * 60 + "\n")
    
    while True:
        try:
            user_input = input("\n🧑 You: ").strip()
            
            if user_input.lower() in ["quit", "exit", "q", "bye"]:
                print("\n👋 Goodbye!")
                break
            
            if user_input.lower() == "clear":
                print("\033[H\033[J", end="")
                continue
            
            if not user_input:
                continue
            
            # Use Coordinator for fused response
            answer = coordinator.chat(user_input)
            print(f"\n🤖 Final Answer:\n{answer}")
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")

def main():
    print("Initializing Multi-Agent System...")
    coordinator = Coordinator()
    interactive_mode(coordinator)
    
    # Forcefully terminate to prevent ROCm hangs on Windows
    import os
    print("\n[System] Chat complete. Forcefully terminating...")
    sys.stdout.flush()
    torch.cuda.empty_cache()
    os._exit(0)

if __name__ == "__main__":
    main()

