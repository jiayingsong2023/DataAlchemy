"""Run one approved compiled SFT snapshot through the existing H6 LoRA Job."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.jobs import validate_gap_base_evaluation
from harness.training_config import prepare_training_context
from release.runtime_tools import register_release_tools
from scripts.run_h5_pdf_cycle import build_runtime
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--base-evaluation-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument(
        "--model-dir", type=Path, required=True, help="Creation-side local model directory"
    )
    parser.add_argument(
        "--training-profile",
        type=Path,
        required=True,
        help="JSON with explicit config and expected worker environment",
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--job-database-url", required=True)
    parser.add_argument("--retry", default="0")
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
    register_release_tools(runtime.tools)
    runtime.verifiers = default_verifiers()
    task = runtime.get_task(str(trial["task_id"]), identity)
    context = {
        "harness_version": 7,
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
        "adapter_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{args.snapshot_id}:adapter:v2")),
        "training_cost_policy": {
            "version": "gpu-hour@1",
            "unit": "gpu_hour",
            "seconds_per_unit": 3600,
        },
        "environment": {
            "classification": "PUBLIC_SYNTHETIC_ENGINEERING",
            "human_reviewed": False,
            "reviewer": "deepseek-v4-pro",
        },
    }
    context = prepare_training_context(
        context, json.loads(args.training_profile.read_text()), args.model_dir
    )
    identity_key = f"{sha256(context)}:{args.retry}"
    context["source_run_id"] = context["run_id"]
    context["run_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ec4-run:{identity_key}"))
    context["adapter_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, identity_key))
    context["output_prefix"] = f"tenants/{args.tenant_id}/adapters/{context['adapter_id']}"
    body = canonical_bytes(context)
    key = f"tenants/{args.tenant_id}/compiler/training-inputs/sha256/{sha256(body)}.json"
    if not store.put_object(key, body, "application/json"):
        raise RuntimeError("compiled_training_context_publish_failed")
    scope = f"raw:{key}"
    training_task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ec4:{args.tenant_id}:{sha256(body)}"))
    try:
        existing = runtime.get_task(training_task_id, identity)
    except PermissionError:
        existing = None
    if existing is not None:
        print(
            json.dumps(
                {
                    "task_id": existing["task_id"],
                    "state": existing["state"],
                    "input_sha256": sha256(body),
                    "reused": True,
                },
                sort_keys=True,
            )
        )
        return
    training_task = runtime.create_task(
        identity,
        "Train frozen compiled SFT candidate (configuration evidence pending EC4-C)",
        [
            {
                "tool": "h5_train_lora",
                "arguments": {"input_key": key, "input_sha256": sha256(body)},
                "scope_refs": [scope],
                "verifier_refs": ["compiled-input", "training-configuration"],
            }
        ],
        max_steps=1,
        task_id=training_task_id,
        run_id=context["run_id"],
        execution_mode="strict",
        task_spec={
            "success_criteria": [
                {
                    "criterion_id": "compiled-input",
                    "verifier": "verify_compile_manifest",
                    "version": 1,
                    "parameters": {
                        "snapshot_id": args.snapshot_id,
                        "compile_manifest_ref": context["compile_manifest_ref"],
                        "compile_manifest_sha256": context["compile_manifest_sha256"],
                    },
                    "phase": "after_step",
                    "required": True,
                },
                {
                    "criterion_id": "training-configuration",
                    "verifier": "verify_training_configuration",
                    "version": 1,
                    "parameters": {},
                    "phase": "after_step",
                    "required": True,
                },
            ],
            "data_scope": {"source_refs": [scope]},
            "limits": {"max_steps": 1, "deadline_seconds": 3600},
        },
    )
    waiting = asyncio.run(runtime.run(training_task["task_id"], identity))
    if waiting["state"] != "waiting_approval":
        raise RuntimeError(f"compiled_training_expected_approval:{waiting['state']}")
    print(
        json.dumps(
            {
                "snapshot_id": args.snapshot_id,
                "task_id": waiting["task_id"],
                "state": waiting["state"],
                "input_ref": key,
                "input_sha256": sha256(body),
                "configuration_sha256": context["effective_training_config_sha256"],
                "next_action": "Review input/profile, approve and resume this task in WebUI",
                "independent_configuration_verification": "required_after_training",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
