"""Run one approved compiled SFT snapshot through the existing H6 LoRA Job."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes, sha256
from core.verifiers import ReadOnlyServices
from harness.jobs import validate_gap_base_evaluation
from scripts.run_h5_pdf_cycle import build_runtime, run_job
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--base-evaluation-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--job-database-url", required=True)
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("compiled_training_database_url_missing")

    identity = {"tenant_id": args.tenant_id, "username": "el2-trainer", "role": "admin"}
    services = ReadOnlyServices(args.database_url, identity)
    snapshot = services.snapshot(args.snapshot_id)
    evaluation = services.evaluation(args.base_evaluation_id)
    if snapshot is None or snapshot["state"] != "approved" or snapshot["algorithm"] != "sft":
        raise ValueError("compiled_training_snapshot_unapproved")
    if evaluation is None:
        raise ValueError("compiled_training_base_evaluation_unpassed")
    validate_gap_base_evaluation(evaluation)
    annotation = services.annotation(snapshot["items"][0]["source_id"])
    trial = services.trial(annotation["trial_id"]) if annotation else None
    if trial is None:
        raise ValueError("compiled_training_source_trial_missing")

    store = S3Utils()
    runtime = build_runtime(args.database_url, store)
    task = runtime.get_task(str(trial["task_id"]), identity)
    context = {
        "harness_version": 6,
        "run_id": task["run_id"],
        **identity,
        "snapshot_id": args.snapshot_id,
        "snapshot_state": "approved",
        "dataset_key": snapshot["dataset_key"],
        "dataset_sha256": snapshot["dataset_sha256"],
        "base_model_digest": snapshot["base_model_digest"],
        "tokenizer_digest": snapshot["target_tokenizer_digest"],
        "chat_template_digest": snapshot["chat_template_digest"],
        "compile_manifest_ref": snapshot["compile_manifest_key"],
        "compile_manifest_sha256": snapshot["compile_manifest_sha256"],
        "model_id": args.model_id,
        "database_url": args.job_database_url,
        "base_evaluation_id": args.base_evaluation_id,
        "base_evaluation_passed": True,
        "output_prefix": f"tenants/{args.tenant_id}/adapters/{args.snapshot_id}",
        "environment": {
            "classification": "PUBLIC_SYNTHETIC_ENGINEERING",
            "human_reviewed": False,
            "reviewer": "deepseek-v4-pro",
        },
    }
    body = canonical_bytes(context)
    key = f"tenants/{args.tenant_id}/compiler/training-inputs/sha256/{sha256(body)}.json"
    if not store.put_object(key, body, "application/json"):
        raise RuntimeError("compiled_training_context_publish_failed")
    result = run_job(
        runtime.jobs,
        task,
        identity,
        kind="lora_train",
        root_run_id=task["run_id"],
        attempt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, args.snapshot_id)),
        gate_name="compiled_sft_lora",
        input_key=key,
        input_sha256=sha256(body),
    )
    print(json.dumps({"snapshot_id": args.snapshot_id, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
