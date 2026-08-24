"""Allowlisted Kubernetes entrypoint for H5 model jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from core.verifiers import default_verifiers
from harness.evaluation import EvaluationService, validate_trial_transcript
from harness.jobs import validate_evaluation_context, validate_training_context
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_json(store: S3Utils, key: str, expected_sha256: str) -> dict[str, Any]:
    body = store.get_object_body(key)
    if body is None or _sha256(body) != expected_sha256:
        raise ValueError("h5_input_hash_mismatch")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("h5_input_manifest_invalid")
    return value


def _write_result(
    store: S3Utils, key: str, job_id: str, input_key: str, input_sha256: str, result: dict[str, Any]
) -> None:
    payload = {
        "job_id": job_id,
        "input_key": input_key,
        "input_sha256": input_sha256,
        "tool_result": result,
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if not store.put_object(key, body, "application/json"):
        raise RuntimeError("h5_result_write_failed")


def finish_evaluation_trials(
    service: EvaluationService,
    store: S3Utils,
    identity: dict[str, str],
    context: dict[str, Any],
    result: dict[str, Any],
) -> None:
    trial_ids = context.get("trial_ids", {})
    task_fingerprints = context.get("task_fingerprints", {})
    receipts = context.get("environment_receipts", {})
    verifier_digest = default_verifiers().get("verify_rag_outcome", 1).contract_digest
    for case in result["output"]["cases"]:
        case_id = case["case_id"]
        trial_id = trial_ids.get(case_id)
        fingerprint = task_fingerprints.get(case_id, {})
        receipt = receipts.get(case_id, {})
        if not trial_id:
            raise ValueError(f"h5_trial_id_missing:{case_id}")
        transcript = validate_trial_transcript(
            {
                "schema_version": "trial_transcript.v1",
                "trial_id": trial_id,
                "case_id": case_id,
                "task_bundle_id": fingerprint.get("task_bundle_id", ""),
                "environment_receipt_ref": receipt.get("ref", ""),
                "environment_receipt_sha256": receipt.get("sha256", ""),
                "prompt": case["prompt"],
                "answer": case["answer"],
                "status": case["status"],
                "citations": case["citations"],
                "latency_ms": case["latency_ms"],
                "model_fingerprint": case["model_fingerprint"],
                "generation_policy": case["generation_policy"],
                "generation_policy_sha256": case["generation_policy_sha256"],
                "verifier": {
                    **case["verification"],
                    "contract_digest": verifier_digest,
                },
            }
        )
        body = json.dumps(
            transcript, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        transcript_key = f"runs/{context['run_id']}/trials/{trial_id}.json"
        transcript_sha256 = _sha256(body)
        if not store.put_object(transcript_key, body, "application/json"):
            raise RuntimeError("h5_transcript_write_failed")
        verification = case["verification"]
        state = {
            "passed": "succeeded",
            "failed": "failed",
            "blocked": "invalidated",
        }[verification["status"]]
        trial_result = {
            "state": state,
            "metrics": {"latency_ms": case["latency_ms"]},
            "model_fingerprint": case["model_fingerprint"],
        }
        if state == "failed":
            trial_result["failure_code"] = verification["error_code"] or "evaluation_gate_failed"
        elif state == "invalidated":
            trial_result["invalid_reason"] = verification["error_code"] or "verifier_blocked"
        service.finish_trial(
            identity,
            trial_id,
            trial_result,
            transcript_key=transcript_key,
            transcript_sha256=transcript_sha256,
        )


def _artifact_digest(path: str) -> tuple[str, int]:
    root = Path(path)
    files = sorted(item for item in root.rglob("*") if item.is_file())
    if not files:
        raise ValueError("h5_adapter_artifact_empty")
    allowed = {".safetensors", ".json"}
    if any(item.suffix not in allowed for item in files):
        raise ValueError("h5_adapter_artifact_format_not_allowed")
    if not any(item.suffix == ".safetensors" for item in files):
        raise ValueError("h5_adapter_safetensors_missing")
    digest = hashlib.sha256()
    size = 0
    for item in files:
        digest.update(item.relative_to(root).as_posix().encode())
        body = item.read_bytes()
        digest.update(body)
        size += len(body)
    return digest.hexdigest(), size


def _safetensors_scan(path: str) -> dict[str, Any]:
    """Reject malformed/non-finite adapter tensors before indexing the manifest."""
    try:
        from safetensors import safe_open
    except ImportError as error:
        raise RuntimeError("h5_safetensors_scanner_missing") from error
    tensors = 0
    for item in Path(path).rglob("*.safetensors"):
        with safe_open(str(item), framework="pt", device="cpu") as handle:
            for key in handle.keys():
                tensor = handle.get_tensor(key)
                if not bool(tensor.isfinite().all()):
                    raise ValueError("h5_adapter_non_finite_tensor")
                tensors += 1
    if not tensors:
        raise ValueError("h5_adapter_tensor_missing")
    return {"passed": True, "scanner": "h5-safetensors-scan-v1", "tensor_count": tensors}


def _stage_candidate_adapter(context: dict[str, Any], store: S3Utils, database_url: str) -> str:
    """Download exactly the candidate recorded for this tenant into the isolated Job."""
    identity = {key: context[key] for key in ("tenant_id", "username", "role")}
    with PostgresDatabase(database_url).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT adapter_id, artifact_key, artifact_sha256, artifact_size, state FROM adapter_manifests "
                "WHERE adapter_id = %s AND tenant_id = %s",
                (context["adapter_id"], context["tenant_id"]),
            )
            adapter = cursor.fetchone()
    if adapter is None or adapter["state"] not in {"candidate", "verified"}:
        raise ValueError("h5_evaluation_adapter_unavailable")
    target = context.get("adapter_path", "/tmp/h5-candidate-adapter")
    if not store.download_directory(adapter["artifact_key"], target):
        raise RuntimeError("h5_evaluation_adapter_download_failed")
    digest, size = _artifact_digest(target)
    if digest != adapter["artifact_sha256"] or size != adapter["artifact_size"]:
        raise ValueError("h5_evaluation_adapter_hash_mismatch")
    return target


def run(
    kind: str, input_key: str, input_sha256: str, result_key: str, job_id: str
) -> dict[str, Any]:
    store = S3Utils()
    context = _read_json(store, input_key, input_sha256)
    expected_tenant = os.getenv("HARNESS_TENANT_ID")
    if expected_tenant and context.get("tenant_id") != expected_tenant:
        raise ValueError("h5_worker_tenant_mismatch")
    if kind == "lora_train":
        context = validate_training_context(context)
        # The heavy ML path is intentionally imported only after all trust-boundary checks.
        from train import train

        training_result = train(context)
        artifact_sha256, artifact_size = _artifact_digest(training_result["adapter_path"])
        safety_scan = _safetensors_scan(training_result["adapter_path"])
        identity = {
            "tenant_id": context["tenant_id"],
            "username": context["username"],
            "role": context["role"],
        }
        database_url = os.getenv("DATABASE_URL") or context["database_url"]
        adapter_id = EvaluationService(database_url).create_adapter_candidate(
            identity,
            snapshot_id=context["snapshot_id"],
            base_model_digest=context["base_model_digest"],
            tokenizer_digest=context["tokenizer_digest"],
            artifact_key=training_result["artifact_prefix"],
            artifact_sha256=artifact_sha256,
            artifact_size=artifact_size,
            config={"format": "safetensors+json", "lora": context.get("lora_config", {})},
            environment=context.get("environment", {}),
            safety_scan=safety_scan,
        )
        result = {
            "output": {
                "snapshot_id": context["snapshot_id"],
                "run_id": context["run_id"],
                "adapter_id": adapter_id,
            },
            "observed_scope": [f"raw:{input_key}"],
            "metrics": {
                "training": "completed",
                "artifact_sha256": artifact_sha256,
                "artifact_size": artifact_size,
            },
        }
    elif kind == "model_evaluate":
        context = validate_evaluation_context(context)
        database_url = os.getenv("DATABASE_URL") or context["database_url"]
        if context.get("use_adapter"):
            context = {
                **context,
                "adapter_path": _stage_candidate_adapter(context, store, database_url),
            }
        from harness.evaluation_runner import run_evaluation

        result = run_evaluation(context)
        identity = {
            "tenant_id": context["tenant_id"],
            "username": context["username"],
            "role": context["role"],
        }
        service = EvaluationService(database_url)
        if context.get("simulation") is not True:
            finish_evaluation_trials(service, store, identity, context, result)
        result["campaign_state"] = service.complete_campaign(
            identity,
            context["evaluation_id"],
            result,
            baseline_evaluation_id=context.get("baseline_evaluation_id"),
        )
    else:
        raise ValueError("h5_job_kind_invalid")
    _write_result(store, result_key, job_id, input_key, input_sha256, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=("lora_train", "model_evaluate"))
    parser.add_argument("--input-key", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--result-key", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        run(args.kind, args.input_key, args.input_sha256, args.result_key, args.job_id)
    except Exception as error:
        print(f"H5 job failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
