import os
import argparse
import sys

# Add current directory to path to allow importing engines and cleaners
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from engines.spark_engine import SparkEngine

# --- Minimal Config ---
DEFAULT_INPUT_PATH = "/mnt/c/Users/Administrator/work/LoRA/data/raw"
DEFAULT_OUTPUT_PATH = "/mnt/c/Users/Administrator/work/LoRA/data"

def main():
    parser = argparse.ArgumentParser(description="Standalone Spark ETL Entry Point")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="Path to raw data")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Path to save output")
    args = parser.parse_args()

    engine = SparkEngine()
    try:
        engine.process_all(args.input, args.output)
    finally:
        engine.stop()

if __name__ == "__main__":
    main()

