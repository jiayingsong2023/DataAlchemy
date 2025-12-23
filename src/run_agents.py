import argparse
import sys
import torch
from agents.coordinator import Coordinator

def main():
    parser = argparse.ArgumentParser(description="Multi-Agent LoRA + RAG Pipeline")
    parser.add_argument("command", choices=["ingest", "train", "chat"], 
                        help="Action to perform: ingest (Agent A+C), train (Agent B), chat (Agent B+C+D)")
    parser.add_argument("--mode", default="python", help="Cleaning engine mode (python/spark)")
    
    args = parser.parse_args()
    coordinator = Coordinator(mode=args.mode)

    if args.command == "ingest":
        coordinator.run_ingestion_pipeline()
    
    elif args.command == "train":
        print("\n" + "=" * 60)
        print("  TRAINING PIPELINE (Agent B)")
        print("=" * 60)
        from train import train
        # Train with current data/train.jsonl
        train()
        
    elif args.command == "chat":
        from inference import main as chat_main
        chat_main()
    
    # Cleanup and force exit to prevent ROCm hangs on Windows
    print("\n[System] Cleaning up GPU resources...")
    if 'coordinator' in locals():
        if hasattr(coordinator, 'agent_b') and coordinator.agent_b:
            del coordinator.agent_b
        if hasattr(coordinator, 'agent_c') and coordinator.agent_c:
            del coordinator.agent_c
            
    torch.cuda.empty_cache()
    print("[System] Task complete. Forcefully terminating to prevent ROCm hang...")
    sys.stdout.flush()
    os._exit(0)

if __name__ == "__main__":
    main()

