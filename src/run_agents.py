import argparse
import sys
import os
import asyncio

# Fix for Windows asyncio loop noise (Option B)
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure src directory is in the path so modules can find 'config'
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

def main():
    print("[System] Initializing Data Alchemy CLI...", flush=True)
    parser = argparse.ArgumentParser(description="Multi-Agent LoRA + RAG Pipeline")
    parser.add_argument("command", choices=["ingest", "train", "chat", "schedule", "full-cycle", "internal-spark-wash"], 
                        help="Action to perform: ingest, train, chat, schedule (Periodic), full-cycle (One-shot)")
    parser.add_argument("--mode", default="python", help="Cleaning engine mode (python/spark)")
    parser.add_argument("--stage", choices=["wash", "refine", "all"], default="all", 
                        help="Ingestion stage: wash (Rough Cleaning), refine (LLM + Indexing), all")
    parser.add_argument("--interval", type=int, default=24, help="Scheduler interval in hours (default: 24)")
    parser.add_argument("--synthesis", action="store_true", help="Enable LLM knowledge synthesis during ingest")
    parser.add_argument("--max_samples", type=int, default=None, help="Max samples for LLM synthesis")
    
    args = parser.parse_args()
    
    # Internal command for WSL-based Spark washing
    # CRITICAL: This path must NOT import torch/coordinator to work in WSL without AI libs
    if args.command == "internal-spark-wash":
        print("[Internal] WSL Spark washing triggered...", flush=True)
        from agents.agent_a import AgentA
        agent_a = AgentA(mode="spark")
        agent_a.clean_and_split()
        sys.exit(0)

    # For all other commands, we lazy load the full AI environment
    def get_coordinator():
        try:
            print("[System] Loading AI components (Torch, Transformers)... This may take a moment on ROCm/Windows.", flush=True)
            import torch
            from agents.coordinator import Coordinator
            return Coordinator(mode=args.mode)
        except ImportError as e:
            print(f"[ERROR] AI libraries not found: {e}")
            print("        Please run: uv sync")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Failed to initialize AI environment: {e}")
            sys.exit(1)

    if args.command == "ingest":
        coordinator = get_coordinator()
        coordinator.run_ingestion_pipeline(
            stage=args.stage,
            synthesis=args.synthesis, 
            max_samples=args.max_samples
        )
    
    elif args.command == "train":
        coordinator = get_coordinator()
        coordinator.run_training_pipeline()
        
    elif args.command == "chat":
        coordinator = get_coordinator() # Although chat_main might load it again, we check here
        from inference import main as chat_main
        chat_main()

    elif args.command == "schedule":
        coordinator = get_coordinator()
        print("\n" + "=" * 60)
        print(f"  AGENT S: ACTIVATED (Interval: {args.interval}h, Synthesis: {args.synthesis})")
        print("=" * 60)
        from agents.agent_scheduler import AgentS
        scheduler = AgentS(coordinator)
        scheduler.start(
            interval_hours=args.interval, 
            synthesis=args.synthesis, 
            max_samples=args.max_samples
        )
    
    elif args.command == "full-cycle":
        coordinator = get_coordinator()
        coordinator.run_full_cycle(
            synthesis=args.synthesis, 
            max_samples=args.max_samples
        )
    
    # Cleanup and force exit to prevent ROCm hangs on Windows
    print("\n[System] Cleaning up GPU resources...", flush=True)
    if 'coordinator' in locals():
        if hasattr(coordinator, 'agent_b') and coordinator.agent_b:
            del coordinator.agent_b
        if hasattr(coordinator, 'agent_c') and coordinator.agent_c:
            del coordinator.agent_c
            
    import torch
    torch.cuda.empty_cache()
    print("[System] Task complete. Forcefully terminating to prevent ROCm hang...")
    sys.stdout.flush()
    os._exit(0)

if __name__ == "__main__":
    main()

