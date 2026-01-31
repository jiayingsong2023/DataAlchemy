import argparse
import os
import sys
import warnings

# Suppress annoying third-party warnings (especially from jieba on Python 3.12)
warnings.filterwarnings("ignore", category=SyntaxWarning, module="jieba")
warnings.filterwarnings("ignore", category=UserWarning, module="jieba")
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

# Ensure src directory is in the path so modules can find 'config'
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

def main():
    from config import validate_config
    validate_config()

    print("[System] Initializing Data Alchemy CLI...", flush=True)
    parser = argparse.ArgumentParser(description="Multi-Agent LoRA + RAG Pipeline")
    parser.add_argument("command", choices=["ingest", "train", "chat", "schedule", "full-cycle", "quant"],
                        help="Action to perform: ingest, train, chat, schedule (Periodic), full-cycle (One-shot), quant (Feature Eng)")
    parser.add_argument("--mode", default="spark", help="Cleaning engine mode (default: spark)")
    parser.add_argument("--stage", choices=["wash", "refine", "all"], default="all",
                        help="Ingestion stage: wash (Rough Cleaning), refine (LLM + Indexing), all")
    parser.add_argument("--interval", type=int, default=24, help="Scheduler interval in hours (default: 24)")
    parser.add_argument("--synthesis", action="store_true", help="Enable LLM knowledge synthesis during ingest")
    parser.add_argument("--max_samples", type=int, default=None, help="Max samples for LLM synthesis")

    # Defaults for quant: try to use the processed path defined in config
    from config import WASHED_DATA_PATH
    default_input = f"{WASHED_DATA_PATH}/metrics.parquet" if WASHED_DATA_PATH.startswith("s3") else "data/processed/metrics.parquet"

    parser.add_argument("--input", default=default_input, help="Input file for numerical quant")
    parser.add_argument("--output", default="data/processed/quant", help="Output directory for numerical quant")

    args = parser.parse_args()

    # For all commands, we lazy load the full AI environment
    def get_coordinator():
        try:
            print("[System] Loading AI components (Torch, Transformers)... This may take a moment on ROCm.", flush=True)
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

    elif args.command == "quant":
        if not args.input:
            print("[ERROR] Numerical quant requires --input <path>")
            sys.exit(1)
        coordinator = get_coordinator()
        coordinator.run_quant_pipeline(args.input, args.output)

    # Cleanup GPU resources
    print("\n[System] Cleaning up GPU resources...", flush=True)
    if 'coordinator' in locals():
        if hasattr(coordinator, 'agent_b') and coordinator.agent_b:
            del coordinator.agent_b
        if hasattr(coordinator, 'agent_c') and coordinator.agent_c:
            del coordinator.agent_c

    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("[System] Task complete.")

if __name__ == "__main__":
    main()

