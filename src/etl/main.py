import argparse
import os

from .engines.spark_engine import SparkEngine

# --- Minimal Config ---
# Get the project root (parent of data_processor)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT_PATH = os.path.join(PROJECT_ROOT, "data", "raw")
DEFAULT_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "processed")

def main():
    parser = argparse.ArgumentParser(description="Cloud-Native Spark ETL Entry Point (S3/MinIO + K8s)")
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

