import argparse
import hashlib
import json
import os

from utils.s3_utils import S3Utils

from .engines.spark_engine import SparkEngine

# --- Minimal Config ---
# Get the project root (parent of data_processor)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT_PATH = os.path.join(PROJECT_ROOT, "data", "raw")
DEFAULT_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "processed")


def _output_hash(output: str) -> str:
    """Hash the exact output prefix after Spark has committed it to MinIO."""
    if not output.startswith("s3a://"):
        raise ValueError("H2 job output must use an s3a:// prefix")
    _bucket, _, prefix = output.removeprefix("s3a://").partition("/")
    objects = S3Utils().list_objects(prefix.rstrip("/") + "/")
    if not objects:
        raise RuntimeError("rough clean produced no output objects")
    digest = hashlib.sha256()
    store = S3Utils()
    for item in sorted(objects, key=lambda value: value["Key"]):
        body = store.get_object_body(item["Key"])
        if body is None:
            raise RuntimeError("rough clean output cannot be read")
        digest.update(item["Key"].encode())
        digest.update(body)
    return digest.hexdigest()


def _rough_metrics(output: str) -> dict[str, int]:
    """Count traceable rough decisions without scanning later RAG products."""
    _bucket, _, prefix = output.removeprefix("s3a://").partition("/")
    records = []
    store = S3Utils()
    for item in store.list_objects(prefix.rstrip("/") + "/cleaned_corpus.jsonl/"):
        if not item["Key"].endswith((".json", ".jsonl")):
            continue
        body = store.get_object_body(item["Key"])
        if body:
            records.extend(json.loads(line) for line in body.decode().splitlines() if line.strip())
    return {
        "records": len(records),
        "accepted": sum(item.get("decision") == "accepted" for item in records),
        "quarantined": sum(item.get("decision") == "quarantined" for item in records),
        "rejected": sum(item.get("decision") == "rejected" for item in records),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Cloud-Native Spark ETL Entry Point (S3/MinIO + K8s)"
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="Path to raw data")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Path to save output")
    parser.add_argument("--result-manifest", help="Run-scoped MinIO key for the H2 job result")
    parser.add_argument("--job-id", help="Server-issued H2 job ID")
    parser.add_argument("--input-sha256", help="Frozen SHA-256 of the exact input")
    args = parser.parse_args()

    engine = SparkEngine()
    try:
        engine.process_all(args.input, args.output)
        if args.result_manifest:
            if not args.job_id or not args.input_sha256:
                raise ValueError("result manifest requires job ID and input SHA-256")
            output_hash = _output_hash(args.output)
            result = {
                "job_id": args.job_id,
                "input_key": args.input,
                "input_sha256": args.input_sha256,
                "tool_result": {
                    "output_prefix": args.output,
                    "observed_scope": [f"raw:{args.input}"],
                    "artifacts": [
                        {
                            "store": "minio",
                            "kind": "cleaned_corpus",
                            "id": args.output,
                            "sha256": output_hash,
                        }
                    ],
                    "metrics": {
                        "output_objects": len(
                            S3Utils().list_objects(
                                args.output.removeprefix("s3a://").partition("/")[2].rstrip("/")
                                + "/"
                            )
                        ),
                        **_rough_metrics(args.output),
                    },
                },
            }
            if not S3Utils().put_object(
                args.result_manifest, json.dumps(result, sort_keys=True), "application/json"
            ):
                raise RuntimeError("could not publish job result manifest")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
