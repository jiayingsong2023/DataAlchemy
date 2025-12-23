"""
Data Alchemy - Dual-Track Architecture
=======================================
Supports two processing modes:
  - python: Pure Python, zero dependencies, ideal for Windows/macOS/demo (< 1GB data)
  - spark:  PySpark distributed processing, ideal for Linux/cloud/production (1GB+ data)
  - auto:   Automatically selects based on platform (Linux -> Spark, Windows/macOS -> Python)

Usage:
  uv run python -m spark_etl.main --mode python   # Force Python engine
  uv run python -m spark_etl.main --mode spark    # Force Spark engine
  uv run python -m spark_etl.main --mode auto     # Auto-detect (default)
  uv run python -m spark_etl.main --sft           # Enable SFT generation
"""
import os
import sys
import json
import argparse
import platform

from spark_etl.config import FINAL_OUTPUT_PATH, SFT_OUTPUT_PATH, RAG_CHUNKS_PATH


def detect_best_engine() -> str:
    """
    Auto-detect the best engine based on platform and environment.
    Returns 'spark' for Linux, 'python' for Windows/macOS.
    """
    system = platform.system().lower()
    
    if system == "linux":
        # Check if we're in a proper Spark environment
        java_home = os.environ.get("JAVA_HOME")
        if java_home and os.path.exists(java_home):
            return "spark"
        else:
            print("[AUTO] Linux detected but JAVA_HOME not set. Using Python engine.")
            return "python"
    elif system == "windows":
        print("[AUTO] Windows detected. Using Python engine (Spark has compatibility issues).")
        return "python"
    elif system == "darwin":
        print("[AUTO] macOS detected. Using Python engine for simplicity.")
        return "python"
    else:
        print(f"[AUTO] Unknown platform '{system}'. Defaulting to Python engine.")
        return "python"


def get_engine(mode: str):
    """
    Factory function to create the appropriate engine.
    
    Args:
        mode: 'python', 'spark', or 'auto'
    
    Returns:
        Engine instance with process_all() and stop() methods.
    """
    if mode == "auto":
        mode = detect_best_engine()
    
    if mode == "spark":
        try:
            from spark_etl.engines.spark_engine import SparkEngine
            return SparkEngine()
        except ImportError as e:
            print(f"[WARN] Spark engine unavailable: {e}")
            print("[WARN] Falling back to Python engine.")
            from spark_etl.engines.python_engine import PythonEngine
            return PythonEngine()
        except Exception as e:
            print(f"[WARN] Failed to initialize Spark: {e}")
            print("[WARN] Falling back to Python engine.")
            from spark_etl.engines.python_engine import PythonEngine
            return PythonEngine()
    else:
        from spark_etl.engines.python_engine import PythonEngine
        return PythonEngine()


def main():
    parser = argparse.ArgumentParser(
        description="Data Alchemy - Dual-Track ETL Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Windows/macOS development (recommended):
  uv run python -m spark_etl.main --mode python
  
  # Linux production with Spark:
  uv run python -m spark_etl.main --mode spark
  
  # Auto-detect based on platform:
  uv run python -m spark_etl.main --mode auto
  
  # Generate SFT data after cleaning:
  uv run python -m spark_etl.main --sft --max_samples 10
        """
    )
    parser.add_argument(
        "--mode", 
        choices=["python", "spark", "auto"],
        default="auto",
        help="Processing engine: 'python' (no deps), 'spark' (distributed), 'auto' (detect)"
    )
    parser.add_argument(
        "--sft", 
        action="store_true",
        help="Generate SFT data using LLM after cleaning"
    )
    parser.add_argument(
        "--rag", 
        action="store_true",
        default=True,
        help="Build RAG index after cleaning (default: True)"
    )
    parser.add_argument(
        "--max_samples", 
        type=int, 
        default=None,
        help="Maximum samples for SFT generation (useful for testing)"
    )
    args = parser.parse_args()

    # Banner
    print("=" * 60)
    print("  Data Alchemy - Dual-Track ETL Pipeline")
    print("=" * 60)
    print(f"  Mode: {args.mode.upper()}")
    print(f"  Platform: {platform.system()} {platform.release()}")
    print("=" * 60)

    # Initialize engine
    engine = get_engine(args.mode)
    
    try:
        # Process all data sources
        results = engine.process_all()
        sft_data = results.get("sft", [])
        rag_data = results.get("rag", [])
        
        if not sft_data and not rag_data:
            print("\n[!] No data found to process.")
            # ... existing print messages ...
            return
        
        # Save SFT results
        if sft_data:
            print(f"\n--- Saving {len(sft_data)} SFT records ---")
            os.makedirs(os.path.dirname(FINAL_OUTPUT_PATH), exist_ok=True)
            with open(FINAL_OUTPUT_PATH, 'w', encoding='utf-8') as f:
                for item in sft_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"SUCCESS! Saved SFT data to: {FINAL_OUTPUT_PATH}")
        
        # Save RAG results
        if rag_data:
            print(f"--- Saving {len(rag_data)} RAG chunks ---")
            os.makedirs(os.path.dirname(RAG_CHUNKS_PATH), exist_ok=True)
            with open(RAG_CHUNKS_PATH, 'w', encoding='utf-8') as f:
                for item in rag_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"SUCCESS! Saved RAG chunks to: {RAG_CHUNKS_PATH}")

            # Build RAG Index via Agent C
            if args.rag:
                print("\n" + "=" * 60)
                print("  Agent C: Building Knowledge Index")
                print("=" * 60)
                try:
                    from agents.agent_c import AgentC
                    agent_c = AgentC()
                    agent_c.build_index(RAG_CHUNKS_PATH)
                except Exception as e:
                    print(f"[ERROR] Agent C failed: {e}")
        
        # Optional: Generate SFT data
        if args.sft:
            print("\n" + "=" * 60)
            print("  SFT Data Generation (LLM-powered)")
            print("=" * 60)
            try:
                from spark_etl.sft_generator import SFTGenerator
                generator = SFTGenerator()
                generator.process_corpus(FINAL_OUTPUT_PATH, max_samples=args.max_samples)
            except Exception as e:
                print(f"[ERROR] SFT generation failed: {e}")
                print("  Hint: Check your API key in spark_etl/config.py")
    
    finally:
        # Cleanup
        engine.stop()


if __name__ == "__main__":
    main()
