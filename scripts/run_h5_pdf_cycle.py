"""Execute a real, PDF-backed H5 snapshot -> LoRA -> evaluation -> release.

This command is intentionally separate from ``run_h5_rehearsal.py``.  It reads
only approved run-bound annotations, creates a governed training snapshot,
submits the allowlisted GPU Jobs, and fails closed on any missing artifact or
gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec
from core.evidence import EvidenceService, S3EvidenceStore
from core.jobs import JobService, KubernetesJobBackend
from core.verifiers import VerificationResult, VerifierRegistry, VerifierSpec
from harness.attempts import AttemptBusy, H5AttemptStore
from harness.evaluation import (
    EvaluationService,
    model_fingerprint_digest,
    validate_suite_manifest,
)
from harness.experience import publish_rag_task_bundle, validate_environment_receipt
from harness.receipts import write_receipt
from release.governance import ReleaseGovernance
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils

_ACTIVE_ATTEMPT: tuple[H5AttemptStore, dict[str, str], str] | None = None


def sha256(body: bytes | str | dict[str, Any]) -> str:
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(body, str):
        body = body.encode()
    return hashlib.sha256(body).hexdigest()


def deterministic_job_key(
    tenant_id: str,
    root_run_id: str,
    h5_attempt_id: str,
    gate_name: str,
    input_sha256: str,
) -> str:
    """Return a retry-stable key; a newly allocated Kubernetes Job is not identity."""
    return sha256(f"{tenant_id}\n{root_run_id}\n{h5_attempt_id}\n{gate_name}\n{input_sha256}")


def upload(store: S3Utils, key: str, body: bytes, content_type: str = "application/json") -> str:
    if not store.put_object(key, body, content_type):
        raise RuntimeError(f"h5_object_write_failed:{key}")
    if store.get_object_body(key) != body:
        raise RuntimeError(f"h5_object_verify_failed:{key}")
    return sha256(body)


def emit_receipt(receipt: dict[str, Any]) -> None:
    path = write_receipt(Path(__file__).resolve().parents[1], str(receipt["run_id"]), receipt)
    receipt["receipt_path"] = str(path)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))


def model_digests(model_dir: Path) -> tuple[str, str]:
    model = model_dir / "model.safetensors"
    tokenizer = model_dir / "tokenizer.model"
    if not model.is_file() or not tokenizer.is_file():
        raise RuntimeError("h5_model_files_missing:model.safetensors,tokenizer.model")
    return sha256(model.read_bytes()), sha256(tokenizer.read_bytes())


def target_fingerprint(
    model_id: str,
    model_dir: Path,
    model_sha256: str,
    tokenizer_sha256: str,
    adapter_sha256: str | None,
) -> dict[str, Any]:
    tokenizer_config = model_dir / "tokenizer_config.json"
    template_sha256 = sha256(tokenizer_config.read_bytes() if tokenizer_config.is_file() else b"")
    return {
        "schema_version": "model_fingerprint.v1",
        "model_id": model_id,
        "model_sha256": model_sha256,
        "tokenizer_sha256": tokenizer_sha256,
        "chat_template_sha256": template_sha256,
        "adapter_sha256": adapter_sha256,
    }


def load_suite(path: Path) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"h5_suite_invalid:{path}") from error
    return validate_suite_manifest(suite)


def snapshot_state(database_url: str, identity: dict[str, str], snapshot_id: str) -> str | None:
    with PostgresDatabase(database_url).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM training_snapshots WHERE snapshot_id = %s AND tenant_id = %s",
                (snapshot_id, identity["tenant_id"]),
            )
            row = cursor.fetchone()
    return row["state"] if row else None


def eligible_annotation_ids(database_url: str, identity: dict[str, str], run_id: str) -> list[str]:
    with PostgresDatabase(database_url).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT annotation_id FROM trajectory_annotations "
                "WHERE tenant_id = %s AND run_id = %s AND status = 'approved' "
                "AND training_allowed = true ORDER BY created_at, annotation_id",
                (identity["tenant_id"], run_id),
            )
            return [str(row["annotation_id"]) for row in cursor.fetchall()]


def load_annotations(
    database_url: str,
    identity: dict[str, str],
    annotation_ids: list[str],
    store: S3Utils,
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    with PostgresDatabase(database_url).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT annotation_id, run_id, tenant_id, content_key, content_sha256, "
                "source_acl_digest, "
                "status, training_allowed, training_purpose, training_permission_version "
                "FROM trajectory_annotations WHERE annotation_id = ANY(%s)",
                (annotation_ids,),
            )
            rows = cursor.fetchall()
    if len(rows) != len(set(annotation_ids)):
        raise RuntimeError("h5_annotation_missing")
    ordered = {str(row["annotation_id"]): row for row in rows}
    result = []
    for annotation_id in annotation_ids:
        row = ordered[annotation_id]
        if str(row["run_id"]) != str(run_id):
            raise RuntimeError(f"h5_annotation_run_mismatch:{annotation_id}")
        if row["tenant_id"] != identity["tenant_id"]:
            raise RuntimeError(f"h5_annotation_tenant_mismatch:{annotation_id}")
        if row["status"] != "approved" or row["training_allowed"] is not True:
            raise RuntimeError(f"h5_annotation_not_approved:{annotation_id}")
        body = store.get_object_body(row["content_key"])
        if body is None or sha256(body) != row["content_sha256"]:
            raise RuntimeError(f"h5_annotation_source_hash_mismatch:{annotation_id}")
        try:
            feedback = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"h5_annotation_source_invalid:{annotation_id}") from error
        if not feedback.get("query") or not feedback.get("answer"):
            raise RuntimeError(f"h5_annotation_training_text_missing:{annotation_id}")
        result.append({**row, "annotation_id": annotation_id, "feedback": feedback})
    return result


def training_dataset(
    annotations: list[dict[str, Any]], tenant_id: str, purpose: str, permission_version: str
) -> tuple[bytes, list[dict[str, Any]]]:
    if len(annotations) < 2:
        raise RuntimeError("h5_requires_two_approved_annotations")
    items = []
    lines = []
    for index, annotation in enumerate(annotations):
        feedback = annotation["feedback"]
        sample = {
            "instruction": feedback["query"],
            "input": "",
            "output": feedback["answer"],
        }
        lines.append(json.dumps(sample, ensure_ascii=False, sort_keys=True))
        items.append(
            {
                "item_id": f"pdf-feedback-{index + 1}",
                "split": "train" if index == 0 else "validation",
                "source_type": "trajectory_annotation",
                "source_id": annotation["annotation_id"],
                "source_sha256": annotation["content_sha256"],
                "source_acl_digest": annotation["source_acl_digest"],
                "training_allowed": True,
                "training_purpose": annotation["training_purpose"] or purpose,
                "training_permission_version": annotation["training_permission_version"]
                or permission_version,
                "transform_digest": sha256(sample),
                "tenant_id": tenant_id,
            }
        )
    return ("\n".join(lines) + "\n").encode(), items


def strict_spec(scope: str) -> dict[str, Any]:
    return {
        "success_criteria": [
            {
                "criterion_id": "h5-trial-contract",
                "verifier": "h5_trial_contract",
                "version": 1,
                "parameters": {},
                "phase": "after_step",
                "required": True,
            }
        ],
        "data_scope": {"source_refs": [scope]},
        "limits": {"max_steps": 1, "deadline_seconds": 3600},
    }


def build_runtime(database_url: str, store: S3Utils) -> AgentRuntime:
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="h5_trial_capture",
            handler=lambda arguments: {
                "case_id": arguments["case_id"],
                "source_run_id": arguments["source_run_id"],
            },
            schema={
                "type": "object",
                "required": ["case_id", "source_run_id"],
                "properties": {"case_id": {"type": "string"}, "source_run_id": {"type": "string"}},
                "additionalProperties": False,
            },
            roles=frozenset({"admin", "reviewer"}),
            result_sensitivity={"case_id": "public", "source_run_id": "internal"},
            scope_resolver=lambda arguments, _identity: [f"run:{arguments['source_run_id']}"],
        )
    )
    verifiers = VerifierRegistry()
    verifiers.register(
        VerifierSpec(
            "h5_trial_contract",
            1,
            lambda *_args: VerificationResult("passed", {"h5_trial": True}),
        )
    )
    evidence_store = S3EvidenceStore(store.bucket, store.client)
    evidence = EvidenceService(database_url, evidence_store, tools.sensitivity)
    jobs = JobService(database_url, KubernetesJobBackend(), evidence_store)
    return AgentRuntime(database_url, tools, verifiers, evidence, jobs)


async def capture_trial(
    runtime: AgentRuntime, identity: dict[str, str], source_run_id: str, case_id: str
) -> dict[str, Any]:
    task = runtime.create_task(
        identity,
        f"H5 evaluation trial: {case_id}",
        [
            {
                "tool": "h5_trial_capture",
                "arguments": {"case_id": case_id, "source_run_id": source_run_id},
                "scope_refs": [f"run:{source_run_id}"],
                "verifier_refs": ["h5-trial-contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(f"run:{source_run_id}"),
    )
    completed = await runtime.run(task["task_id"], identity)
    if completed["state"] != "succeeded":
        raise RuntimeError(f"h5_trial_failed:{case_id}:{completed.get('finish_reason')}")
    return completed


def run_job(
    service: JobService,
    task: dict[str, Any],
    identity: dict[str, str],
    *,
    kind: str,
    root_run_id: str,
    attempt_id: str,
    gate_name: str,
    input_key: str,
    input_sha256: str,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    step = {
        "step_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                deterministic_job_key(
                    identity["tenant_id"], root_run_id, attempt_id, gate_name, input_sha256
                ),
            )
        ),
        "job_kind": kind,
        "arguments": {
            "input_key": input_key,
            "input_sha256": input_sha256,
            "idempotency_key": deterministic_job_key(
                identity["tenant_id"], root_run_id, attempt_id, gate_name, input_sha256
            ),
            "deadline_seconds": 3600,
        },
    }
    job = service.request(task, step, identity)
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        observation = service.reconcile(job, identity)
        if observation.state == "succeeded":
            if not isinstance(observation.result, dict):
                raise RuntimeError(f"h5_job_result_missing:{kind}")
            return observation.result
        if observation.state in {"failed", "cancelled", "orphaned"}:
            raise RuntimeError(f"h5_job_failed:{kind}:{observation.error_code}")
        time.sleep(5)
        job = service.get(job["job_id"], identity)
    raise RuntimeError(f"h5_job_timeout:{kind}")


def main() -> None:  # noqa: C901 - one auditable gate sequence
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--annotation-ids", help="Comma-separated approved annotation IDs")
    parser.add_argument("--annotation-id", action="append", default=[])
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--attempt-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--environment", choices=("production", "engineering"), default="production"
    )
    parser.add_argument("--allow-auto-approve", action="store_true")
    parser.add_argument("--canary-observation", type=Path)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.getenv("HARNESS_JOB_MODEL_HOST_PATH", "data/models/TinyLlama")),
    )
    parser.add_argument("--policy-version", default="pdf-h5-v1")
    parser.add_argument("--permission-version", default="pdf-cycle-v1")
    parser.add_argument("--task-retention-until")
    parser.add_argument("--environment-receipts", type=Path)
    parser.add_argument("--promote", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    required = ("DATABASE_URL", "S3_ENDPOINT", "HARNESS_JOB_NAMESPACE", "HARNESS_JOB_IMAGE")
    if missing := [name for name in required if not os.getenv(name)]:
        raise RuntimeError(f"h5_environment_missing:{','.join(missing)}")
    if args.allow_auto_approve and args.environment != "engineering" and not args.resume:
        raise RuntimeError("auto_approve_requires_engineering_environment")
    if args.environment == "production" and args.promote:
        raise RuntimeError("production_promote_requires_release_approval")
    database_url = os.environ["DATABASE_URL"]
    owner = {
        "tenant_id": args.tenant_id,
        "username": os.getenv("H5_RUNNER_USERNAME", "h5-runner"),
        "role": "admin",
    }
    reviewer = {
        "tenant_id": args.tenant_id,
        "username": os.getenv("H5_REVIEWER_USERNAME", "h5-reviewer"),
        "role": "reviewer",
    }
    promoter = {
        "tenant_id": args.tenant_id,
        "username": os.getenv("H5_PROMOTER_USERNAME", "h5-promoter"),
        "role": "admin",
    }
    if len({owner["username"], reviewer["username"], promoter["username"]}) != 3:
        raise RuntimeError("h5_maker_checker_identities_must_differ")
    attempts = H5AttemptStore(database_url)
    annotation_ids = list(args.annotation_id)
    if args.annotation_ids:
        annotation_ids.extend(value for value in args.annotation_ids.split(",") if value)
    if args.resume:
        if not args.attempt_id:
            active = attempts.active(owner, args.run_id)
            if active is None:
                raise RuntimeError("h5_active_attempt_missing_for_resume")
            args.attempt_id = str(active["attempt_id"])
        existing = attempts.get(owner, args.attempt_id)
        persisted = existing["config_json"]
        if str(existing["run_id"]) != str(args.run_id):
            raise RuntimeError("h5_attempt_run_mismatch")
        if str(existing["tenant_id"]) != str(args.tenant_id):
            raise RuntimeError("h5_attempt_tenant_mismatch")
        annotation_ids = list(persisted["annotation_ids"])
        suite = validate_suite_manifest(persisted["suite"])
        if args.suite and sha256(load_suite(args.suite)) != sha256(suite):
            raise RuntimeError("h5_attempt_config_mismatch")
        args.environment = persisted["environment"]
        args.policy_version = persisted["policy_version"]
        args.permission_version = persisted["permission_version"]
        args.task_retention_until = persisted.get("task_retention_until")
        if not args.task_retention_until:
            raise RuntimeError("h5_attempt_task_retention_missing")
        args.model_dir = Path(persisted["model_dir"])
        environment_receipts = persisted.get("environment_receipts")
        if args.allow_auto_approve and args.environment != "engineering":
            raise RuntimeError("auto_approve_requires_engineering_environment")
        if existing.get("release_id"):
            release = ReleaseGovernance(database_url)
            release_state = release.status(str(existing["release_id"]), owner)
            if release_state == "promoted":
                emit_receipt(
                    {
                        "status": "passed",
                        "run_id": args.run_id,
                        "h5_attempt_id": str(existing["attempt_id"]),
                        "snapshot_id": existing.get("snapshot_id"),
                        "base_evaluation_id": existing.get("base_evaluation_id"),
                        "candidate_evaluation_id": existing.get("candidate_evaluation_id"),
                        "adapter_id": existing.get("adapter_id"),
                        "release_id": existing.get("release_id"),
                        "resumed": True,
                    }
                )
                return
        if existing["state"] == "passed":
            emit_receipt(
                {
                    "status": "passed",
                    "run_id": args.run_id,
                    "h5_attempt_id": str(existing["attempt_id"]),
                    "snapshot_id": existing.get("snapshot_id"),
                    "base_evaluation_id": existing.get("base_evaluation_id"),
                    "candidate_evaluation_id": existing.get("candidate_evaluation_id"),
                    "adapter_id": existing.get("adapter_id"),
                    "release_id": existing.get("release_id"),
                    "resumed": True,
                }
            )
            return
    else:
        if not args.suite:
            raise RuntimeError("h5_suite_required_for_attempt_creation")
        if not args.task_retention_until:
            raise RuntimeError("h5_task_retention_until_required")
        try:
            retention = datetime.fromisoformat(args.task_retention_until.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError("h5_task_retention_until_invalid") from error
        if retention.tzinfo is None:
            raise RuntimeError("h5_task_retention_timezone_missing")
        if not annotation_ids and args.environment == "engineering":
            annotation_ids = eligible_annotation_ids(database_url, owner, args.run_id)
        if not annotation_ids and args.environment == "production":
            emit_receipt(
                {
                    "state": "waiting_input",
                    "reason": "explicit_annotation_ids_required_in_production",
                    "run_id": args.run_id,
                }
            )
            return
        if len(annotation_ids) < 2:
            emit_receipt(
                {
                    "state": "waiting_input",
                    "reason": "two_approved_annotations_required",
                    "run_id": args.run_id,
                }
            )
            return
        suite = load_suite(args.suite)
        if not args.environment_receipts:
            raise RuntimeError("h5_environment_receipts_required")
        try:
            environment_receipts = json.loads(args.environment_receipts.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("h5_environment_receipts_invalid") from error
    store = S3Utils()
    store.ensure_bucket()
    annotations = load_annotations(database_url, owner, annotation_ids, store, run_id=args.run_id)
    attempt_config = {
        "run_id": args.run_id,
        "tenant_id": args.tenant_id,
        "annotation_ids": annotation_ids,
        "annotation_hashes": {
            item["annotation_id"]: item["content_sha256"] for item in annotations
        },
        "suite": suite,
        "suite_sha256": sha256(suite),
        "policy_version": args.policy_version,
        "permission_version": args.permission_version,
        "task_retention_until": args.task_retention_until,
        "environment": args.environment,
        "model_dir": str(args.model_dir),
        "environment_receipts": environment_receipts,
    }
    attempt = attempts.create_or_load(owner, args.run_id, attempt_config, args.attempt_id)
    attempt_id = str(attempt["attempt_id"])
    global _ACTIVE_ATTEMPT
    _ACTIVE_ATTEMPT = (attempts, owner, attempt_id)
    lease_owner = f"{owner['username']}:{uuid.uuid4()}"
    try:
        attempts.acquire(owner, attempt_id, lease_owner)
    except AttemptBusy:
        emit_receipt(
            {"state": "already_running", "run_id": args.run_id, "h5_attempt_id": attempt_id}
        )
        return
    attempts.state(owner, attempt_id, "running", gate="training_snapshot")
    heartbeat = lambda: attempts.renew(owner, attempt_id, lease_owner)
    prefix = f"runs/{args.run_id}/h5"
    dataset_key = f"{prefix}/training/dataset.jsonl"
    base_model_digest, tokenizer_digest = model_digests(args.model_dir)
    evaluations = EvaluationService(database_url)
    automated_approval = args.environment == "engineering" and args.allow_auto_approve
    snapshot_id = attempt.get("snapshot_id")
    if snapshot_id and args.resume:
        dataset_body = store.get_object_body(dataset_key)
        if dataset_body is None:
            raise RuntimeError("h5_snapshot_dataset_missing")
        dataset_sha256 = sha256(dataset_body)
        attempts.gate(
            owner,
            args.run_id,
            attempt_id,
            "training_snapshot",
            "passed",
            evidence={"snapshot_id": str(snapshot_id), "resumed": True},
        )
    else:
        dataset_body, snapshot_items = training_dataset(
            annotations, args.tenant_id, "pdf_qa_improvement", args.permission_version
        )
        dataset_sha256 = upload(store, dataset_key, dataset_body)
        snapshot_id = evaluations.create_snapshot(
            owner,
            annotation_items=snapshot_items,
            dataset_key=dataset_key,
            dataset_sha256=dataset_sha256,
            dataset_size=len(dataset_body),
            base_model_digest=base_model_digest,
            policy_version=args.policy_version,
        )
        attempts.refs(owner, attempt_id, snapshot_id=snapshot_id)
        attempts.gate(
            owner,
            args.run_id,
            attempt_id,
            "training_snapshot",
            "running",
            output_artifact_id=dataset_key,
            output_sha256=dataset_sha256,
            evidence={"snapshot_id": snapshot_id},
        )
    if (
        not automated_approval
        and snapshot_state(database_url, reviewer, str(snapshot_id)) != "approved"
    ):
        attempts.state(owner, attempt_id, "waiting_approval", gate="training_snapshot")
        attempts.gate(
            owner,
            args.run_id,
            attempt_id,
            "training_snapshot",
            "waiting_approval",
            evidence={"snapshot_id": snapshot_id, "approval": "reviewer_required"},
        )
        emit_receipt(
            {
                "state": "waiting_approval",
                "approval_type": "snapshot",
                "run_id": args.run_id,
                "h5_attempt_id": attempt_id,
                "snapshot_id": snapshot_id,
                "next_action": "approve in WebUI, then rerun with --resume",
            }
        )
        return
    if not args.resume:
        evaluations.approve_snapshot(reviewer, snapshot_id)
    attempts.gate(
        owner,
        args.run_id,
        attempt_id,
        "training_snapshot",
        "passed",
        evidence={"snapshot_id": snapshot_id},
    )

    runtime = build_runtime(database_url, store)
    job_service = runtime.jobs
    assert job_service is not None
    suite_sha256 = sha256(validate_suite_manifest(suite))
    task_store = S3EvidenceStore(store.bucket, store.client)
    reset_script = Path(__file__).with_name("reset_pilot_environment.py")
    reset_contract = {
        "kind": "registered-script",
        "ref": "scripts/reset_pilot_environment.py",
        "sha256": sha256(reset_script.read_bytes()),
    }
    tool = runtime.tools.get("h5_trial_capture")
    tool_contract = {
        "name": tool.name,
        "version": tool.version,
        "contract_sha256": tool.contract_digest,
    }
    environment_snapshot = {
        "schema_version": "h5_pdf_environment.v1",
        "source_run_id": args.run_id,
        "suite_sha256": suite_sha256,
        "source": suite.get("source"),
        "environment": args.environment,
    }
    acl_sha256 = sha256(
        {"source_acl_digests": sorted(item["source_acl_digest"] for item in annotations)}
    )
    task_assets = {
        case["case_id"]: publish_rag_task_bundle(
            task_store,
            {**case, **({"source": suite["source"]} if suite.get("source") else {})},
            tenant_id=args.tenant_id,
            environment_snapshot=environment_snapshot,
            reset_contract=reset_contract,
            tool_contract=tool_contract,
            verifier_name="verify_rag_outcome",
            verifier_version=1,
            limits={"max_steps": 1, "deadline_seconds": 3600},
            acl_sha256=acl_sha256,
            permission_version=args.permission_version,
            retention_until=args.task_retention_until,
        )
        for case in suite["cases"]
    }
    if not isinstance(environment_receipts, dict) or set(environment_receipts) != set(task_assets):
        raise RuntimeError("h5_environment_receipt_coverage_mismatch")
    for case_id, descriptor in environment_receipts.items():
        if not isinstance(descriptor, dict) or set(descriptor) != {"ref", "sha256"}:
            raise RuntimeError("h5_environment_receipt_descriptor_invalid")
        body = store.get_object_body(descriptor["ref"])
        if body is None or sha256(body) != descriptor["sha256"]:
            raise RuntimeError("h5_environment_receipt_hash_mismatch")
        receipt = validate_environment_receipt(json.loads(body))
        if (
            receipt["state"] != "ready"
            or receipt["task_bundle_id"] != task_assets[case_id]["fingerprint"]["task_bundle_id"]
        ):
            raise RuntimeError("h5_environment_receipt_not_ready")
    base_evaluation = evaluations.create_campaign(
        owner,
        suite,
        subject_type="base",
        subject_ref=base_model_digest,
        required_trials=len(suite["cases"]),
    )
    evaluation_model_id = os.getenv("H5_MODEL_ID", "/app/data/models/TinyLlama")
    base_fingerprint = target_fingerprint(
        evaluation_model_id, args.model_dir, base_model_digest, tokenizer_digest, None
    )
    base_tasks = []
    base_trial_ids = {}
    for number, case in enumerate(suite["cases"], 1):
        task = asyncio.run(capture_trial(runtime, owner, args.run_id, case["case_id"]))
        trial_id = evaluations.register_trial(
            owner,
            base_evaluation,
            task,
            case_id=case["case_id"],
            trial_no=number,
            fingerprint={
                **task_assets[case["case_id"]]["fingerprint"],
                "source_run_id": args.run_id,
                "model": "base",
                "model_fingerprint_sha256": model_fingerprint_digest(base_fingerprint),
            },
        )
        base_trial_ids[case["case_id"]] = trial_id
        base_tasks.append(task)
    generation_policy = {
        "max_new_tokens": int(os.getenv("H5_MAX_NEW_TOKENS", "64")),
        "do_sample": False,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    base_context = {
        "harness_version": 5,
        "run_id": args.run_id,
        "tenant_id": args.tenant_id,
        "username": owner["username"],
        "role": owner["role"],
        "evaluation_id": base_evaluation,
        "suite_sha256": suite_sha256,
        "database_url": os.getenv("HARNESS_JOB_DATABASE_URL", database_url),
        "model_id": evaluation_model_id,
        "cases": [task_assets[case["case_id"]]["model_input"] for case in suite["cases"]],
        "verifier_cases": [
            task_assets[case["case_id"]]["verifier_input"] for case in suite["cases"]
        ],
        "max_new_tokens": generation_policy["max_new_tokens"],
        "generation_policy": generation_policy,
        "generation_policy_sha256": sha256(generation_policy),
        "model_fingerprint": base_fingerprint,
        "trial_ids": base_trial_ids,
        "task_fingerprints": {
            case_id: asset["fingerprint"] for case_id, asset in task_assets.items()
        },
        "environment_receipts": environment_receipts,
    }
    base_key = f"{prefix}/jobs/base-evaluation.json"
    base_hash = upload(store, base_key, json.dumps(base_context, sort_keys=True).encode())
    base_result = run_job(
        job_service,
        base_tasks[0],
        owner,
        kind="model_evaluate",
        root_run_id=args.run_id,
        attempt_id=attempt_id,
        gate_name="base_evaluation",
        input_key=base_key,
        input_sha256=base_hash,
        heartbeat=heartbeat,
    )
    if base_result.get("campaign_state") != "passed":
        raise RuntimeError("h5_base_evaluation_failed")

    training_context = {
        "harness_version": 5,
        "run_id": args.run_id,
        "tenant_id": args.tenant_id,
        "username": owner["username"],
        "role": owner["role"],
        "snapshot_id": snapshot_id,
        "snapshot_state": "approved",
        "dataset_key": dataset_key,
        "dataset_sha256": dataset_sha256,
        "base_model_digest": base_model_digest,
        "tokenizer_digest": tokenizer_digest,
        "model_id": os.getenv("H5_MODEL_ID", "/app/data/models/TinyLlama"),
        "database_url": os.getenv("HARNESS_JOB_DATABASE_URL", database_url),
        "base_evaluation_id": base_evaluation,
        "base_evaluation_passed": True,
        "output_prefix": f"{prefix}/adapters",
        "environment": {"source_run_id": args.run_id, "pdf_backed": True},
    }
    training_key = f"{prefix}/jobs/lora-train.json"
    training_hash = upload(
        store, training_key, json.dumps(training_context, sort_keys=True).encode()
    )
    training_result = run_job(
        job_service,
        base_tasks[0],
        owner,
        kind="lora_train",
        root_run_id=args.run_id,
        attempt_id=attempt_id,
        gate_name="lora",
        input_key=training_key,
        input_sha256=training_hash,
        heartbeat=heartbeat,
    )
    adapter_id = training_result.get("output", {}).get("adapter_id")
    if not adapter_id:
        raise RuntimeError("h5_adapter_id_missing")
    adapter_artifact_sha256 = training_result.get("metrics", {}).get("artifact_sha256")

    candidate_evaluation = evaluations.create_campaign(
        owner,
        suite,
        subject_type="adapter",
        subject_ref=adapter_id,
        required_trials=len(suite["cases"]),
    )
    candidate_fingerprint = target_fingerprint(
        evaluation_model_id,
        args.model_dir,
        base_model_digest,
        tokenizer_digest,
        adapter_artifact_sha256,
    )
    candidate_tasks = []
    candidate_trial_ids = {}
    for number, case in enumerate(suite["cases"], 1):
        task = asyncio.run(capture_trial(runtime, owner, args.run_id, case["case_id"]))
        trial_id = evaluations.register_trial(
            owner,
            candidate_evaluation,
            task,
            case_id=case["case_id"],
            trial_no=number,
            fingerprint={
                **task_assets[case["case_id"]]["fingerprint"],
                "source_run_id": args.run_id,
                "adapter_id": adapter_id,
                "model_fingerprint_sha256": model_fingerprint_digest(candidate_fingerprint),
            },
        )
        candidate_trial_ids[case["case_id"]] = trial_id
        candidate_tasks.append(task)
    candidate_context = {
        **base_context,
        "run_id": args.run_id,
        "evaluation_id": candidate_evaluation,
        "use_adapter": True,
        "adapter_id": adapter_id,
        "baseline_evaluation_id": base_evaluation,
        "model_fingerprint": candidate_fingerprint,
        "trial_ids": candidate_trial_ids,
    }
    candidate_key = f"{prefix}/jobs/adapter-evaluation.json"
    candidate_hash = upload(
        store, candidate_key, json.dumps(candidate_context, sort_keys=True).encode()
    )
    candidate_result = run_job(
        job_service,
        candidate_tasks[0],
        owner,
        kind="model_evaluate",
        root_run_id=args.run_id,
        attempt_id=attempt_id,
        gate_name="adapter_evaluation",
        input_key=candidate_key,
        input_sha256=candidate_hash,
        heartbeat=heartbeat,
    )
    if candidate_result.get("campaign_state") != "passed":
        raise RuntimeError("h5_adapter_evaluation_failed")
    evaluations.verify_adapter(reviewer, adapter_id, candidate_evaluation)
    attempts.refs(
        owner,
        attempt_id,
        adapter_id=adapter_id,
        candidate_evaluation_id=candidate_evaluation,
    )

    observation: dict[str, Any]
    if args.canary_observation:
        try:
            observation = json.loads(args.canary_observation.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("h5_canary_observation_invalid") from error
        required_observation = {
            "sample_count",
            "window_seconds",
            "security_passed",
            "window_complete",
            "error_rate",
            "p95_ms",
        }
        if not required_observation <= observation.keys():
            raise RuntimeError("h5_canary_observation_incomplete")
        classification = "PDF_BACKED_REAL_INFRASTRUCTURE"
    elif args.environment == "production":
        attempts.state(owner, attempt_id, "waiting_input", gate="release")
        emit_receipt(
            {
                "state": "waiting_input",
                "reason": "real_canary_observation_required",
                "run_id": args.run_id,
                "h5_attempt_id": attempt_id,
                "next_action": "provide --canary-observation from the measured traffic window",
            }
        )
        return
    else:
        observation = {
            "sample_count": 1,
            "window_seconds": 1,
            "security_passed": True,
            "window_complete": True,
            "error_rate": 0.0,
            "p95_ms": 1,
        }
        classification = "PDF_BACKED_ENGINEERING_REHEARSAL"

    governance = ReleaseGovernance(database_url)
    min_samples = "10" if args.environment == "production" else "1"
    window_seconds = "300" if args.environment == "production" else "1"
    manifest = {
        "harness_version": 5,
        "code_version": os.getenv("BUILD_GIT_SHA", "local"),
        "adapter_id": adapter_id,
        "evaluation_id": candidate_evaluation,
        "training_snapshot_id": snapshot_id,
        "evaluation": {"passed": True, "hard_gates_passed": True},
        "rollback_to": "base",
        "guardrails": {
            "max_error_rate": 0.01,
            "max_p95_ms": 2000,
            "min_samples": int(os.getenv("H5_CANARY_MIN_SAMPLES", min_samples)),
            "window_seconds": int(os.getenv("H5_CANARY_WINDOW_SECONDS", window_seconds)),
        },
        "release_scope": "single_tenant_lora",
        "approvals": {"candidate": owner["username"], "promote": promoter["username"]},
        "policy_version": args.policy_version,
        "source_run_id": args.run_id,
        "pdf_backed": True,
    }
    release_id = governance.create_candidate(owner, manifest)
    attempts.refs(owner, attempt_id, release_id=release_id)
    governance.advance(release_id, "shadow", owner)
    governance.advance(release_id, "canary", owner)
    release_status = governance.observe(
        release_id,
        observation,
        promoter,
        promote=args.environment == "engineering",
    )
    if release_status == "awaiting_promotion":
        attempts.state(owner, attempt_id, "waiting_approval", gate="release")
        attempts.gate(
            owner,
            args.run_id,
            attempt_id,
            "release",
            "waiting_approval",
            output_artifact_id=release_id,
            evidence={"classification": classification, "observation": observation},
        )
        emit_receipt(
            {
                "state": "waiting_approval",
                "approval_type": "release",
                "run_id": args.run_id,
                "h5_attempt_id": attempt_id,
                "release_id": release_id,
                "adapter_id": adapter_id,
                "next_action": "approve promotion in WebUI, then rerun with --resume",
            }
        )
        return
    if release_status != "promoted":
        raise RuntimeError(f"h5_release_not_promoted:{release_status}")
    attempts.gate(
        owner,
        args.run_id,
        attempt_id,
        "release",
        "passed",
        output_artifact_id=release_id,
        evidence={"classification": classification, "observation": observation},
    )
    attempts.state(owner, attempt_id, "passed", gate="release")
    emit_receipt(
        {
            "status": "passed",
            "run_id": args.run_id,
            "classification": classification,
            "source_run_id": args.run_id,
            "snapshot_id": snapshot_id,
            "base_evaluation_id": base_evaluation,
            "candidate_evaluation_id": candidate_evaluation,
            "adapter_id": adapter_id,
            "adapter_artifact_sha256": adapter_artifact_sha256,
            "release_id": release_id,
            "dataset_key": dataset_key,
            "dataset_sha256": dataset_sha256,
        }
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, PermissionError) as error:
        if _ACTIVE_ATTEMPT is not None:
            store, identity, attempt_id = _ACTIVE_ATTEMPT
            try:
                store.state(identity, attempt_id, "failed", error_code=type(error).__name__)
            except Exception:
                pass
        print(f"ERROR: {error}", flush=True)
        raise SystemExit(2) from error
