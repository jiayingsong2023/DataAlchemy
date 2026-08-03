"""Run a real-infrastructure H5 rehearsal with synthetic, non-production data.

Required environment: DATABASE_URL, VERIFIER_DATABASE_URL, S3_ENDPOINT and the
HARNESS_JOB_* values accepted by KubernetesJobBackend.  The script never marks
H5 closed: its report is explicitly labelled SIMULATION.
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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec
from src.core.evidence import EvidenceService, S3EvidenceStore
from src.core.jobs import JobService, KubernetesJobBackend
from src.core.verifiers import VerificationResult, VerifierRegistry, VerifierSpec
from src.harness.evaluation import EvaluationService, validate_suite_manifest
from src.release.governance import ReleaseGovernance
from src.utils.s3_utils import S3Utils


def digest(body: bytes | str | dict[str, Any]) -> str:
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(body, str):
        body = body.encode()
    return hashlib.sha256(body).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def strict_spec(scope: str) -> dict[str, Any]:
    return {
        "success_criteria": [
            {
                "criterion_id": "contract",
                "verifier": "contract",
                "version": 1,
                "parameters": {},
                "phase": "after_step",
                "required": True,
            }
        ],
        "data_scope": {"source_refs": [scope]},
        "limits": {"max_steps": 1, "deadline_seconds": 3600},
    }


def upload(store: S3Utils, key: str, body: bytes) -> str:
    if not store.put_object(key, body, "application/json") or store.get_object_body(key) != body:
        raise RuntimeError(f"rehearsal_object_write_failed:{key}")
    return digest(body)


async def run_capture(runtime: AgentRuntime, identity: dict[str, str], scope: str, number: int) -> dict[str, Any]:
    task = runtime.create_task(
        identity,
        f"SIMULATION evaluation trial {number}",
        [{"tool": "capture", "arguments": {"trial": number}, "scope_refs": [scope], "verifier_refs": ["contract"]}],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(scope),
    )
    completed = await runtime.run(task["task_id"], identity)
    if completed["state"] != "succeeded" or not runtime.evidence_status(task["task_id"], identity):
        raise RuntimeError("rehearsal_trial_evidence_missing")
    return task


async def run_job(
    runtime: AgentRuntime,
    identity: dict[str, str],
    *,
    tool: str,
    input_key: str,
    input_sha256: str,
    scope: str,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    arguments = {"input_key": input_key, "input_sha256": input_sha256, **(extra or {})}
    task = runtime.create_task(
        identity,
        f"SIMULATION {tool}",
        [{"tool": tool, "arguments": arguments, "scope_refs": [scope], "verifier_refs": ["contract"]}],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(scope),
    )
    waiting = await runtime.run(task["task_id"], identity)
    if waiting["state"] != "waiting_approval":
        raise RuntimeError("rehearsal_job_approval_missing")
    runtime.approve(task["task_id"], identity, True, waiting["version"])
    current = await runtime.run(task["task_id"], identity)
    deadline = time.monotonic() + 1800
    while current["state"] == "waiting_job" and time.monotonic() < deadline:
        await asyncio.sleep(5)
        current = await runtime.reconcile_job(task["task_id"], identity, current["version"])
    if current["state"] != "succeeded":
        raise RuntimeError(f"rehearsal_job_failed:{current['state']}:{current.get('finish_reason')}")
    job = runtime.jobs.for_task(task, identity)
    if job is None or not job.get("result_key"):
        raise RuntimeError("rehearsal_job_result_missing")
    return task, runtime.jobs.result_store.get(job["result_key"])  # type: ignore[union-attr]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="data/models/TinyLlama")
    args = parser.parse_args()
    required = ("DATABASE_URL", "VERIFIER_DATABASE_URL", "S3_ENDPOINT", "HARNESS_JOB_NAMESPACE", "HARNESS_JOB_IMAGE")
    if missing := [name for name in required if not os.getenv(name)]:
        raise RuntimeError(f"rehearsal_environment_missing:{','.join(missing)}")
    model_dir = Path(args.model_dir)
    if not (model_dir / "model.safetensors").is_file() or not (model_dir / "tokenizer.model").is_file():
        raise RuntimeError("rehearsal_local_model_missing")

    tenant = f"h5-simulation-{uuid.uuid4()}"
    owner = {"tenant_id": tenant, "username": "simulation-runner", "role": "admin"}
    reviewer = {"tenant_id": tenant, "username": "simulation-reviewer", "role": "reviewer"}
    promoter = {"tenant_id": tenant, "username": "simulation-promoter", "role": "admin"}
    store = S3Utils()
    store.ensure_bucket()
    evidence = S3EvidenceStore(store.bucket, store.client)
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="capture",
            handler=lambda arguments: {"trial": arguments["trial"], "observed_scope": [f"simulation:{tenant}:trial"]},
            schema={"type": "object", "required": ["trial"], "properties": {"trial": {"type": "integer"}}, "additionalProperties": False},
            scope_resolver=lambda _arguments, _identity: [f"simulation:{tenant}:trial"],
            result_sensitivity={"trial": "public"},
        )
    )
    for name, kind, schema, scope_for in (
        ("h5_train_lora", "lora_train", {"type": "object", "required": ["input_key", "input_sha256"], "properties": {"input_key": {"type": "string"}, "input_sha256": {"type": "string"}}, "additionalProperties": False}, lambda arguments, _identity: [f"raw:{arguments['input_key']}"]),
        ("h5_model_evaluate", "model_evaluate", {"type": "object", "required": ["input_key", "input_sha256", "evaluation_id"], "properties": {"input_key": {"type": "string"}, "input_sha256": {"type": "string"}, "evaluation_id": {"type": "string"}}, "additionalProperties": False}, lambda arguments, _identity: [f"evaluation:{arguments['evaluation_id']}"]),
    ):
        tools.register(
            ToolSpec(
                name=name,
                handler=lambda _arguments: (_ for _ in ()).throw(RuntimeError("job_only")),
                schema=schema,
                roles=frozenset({"admin"}),
                requires_approval=True,
                idempotent=True,
                side_effecting=True,
                uses_identity=True,
                scope_resolver=scope_for,
                execution="kubernetes_job",
                job_kind=kind,
                result_sensitivity={"*": "internal"},
            )
        )
    verifiers = VerifierRegistry()
    verifiers.register(VerifierSpec("contract", 1, lambda *_: VerificationResult("passed", {"simulation": True})))
    database_url = os.environ["DATABASE_URL"]
    job_service = JobService(database_url, KubernetesJobBackend(), evidence)
    runtime = AgentRuntime(database_url, tools, verifiers, EvidenceService(database_url, evidence, tools.sensitivity), job_service)
    evaluations = EvaluationService(database_url)
    prefix = f"simulation/h5/{tenant}"
    base_digest = file_digest(model_dir / "model.safetensors")
    tokenizer_digest = file_digest(model_dir / "tokenizer.model")
    suite = {"version": "h5-simulation-v1", "policy_version": "h5-simulation-v1", "cases": [{"case_id": "basic-generation", "input_sha256": digest("Give a brief acknowledgement."), "query": "Give a brief acknowledgement.", "required_substrings": []}]}

    base_evaluation = evaluations.create_campaign(owner, suite, subject_type="base", subject_ref=base_digest, required_trials=3)
    trials = []
    for number in range(1, 4):
        trial = asyncio.run(run_capture(runtime, owner, f"simulation:{tenant}:trial", number))
        trial_id = evaluations.register_trial(owner, base_evaluation, trial, case_id="basic-generation", trial_no=number, fingerprint={"simulation": True})
        transcript = json.dumps({"simulation": True, "trial": number}).encode()
        transcript_key = f"{prefix}/trials/{trial['run_id']}.json"
        evaluations.finish_trial(owner, trial_id, {"state": "succeeded", "metrics": {"simulation": True}}, transcript_key=transcript_key, transcript_sha256=upload(store, transcript_key, transcript))
        trials.append((trial, trial_id))
    base_input = {"harness_version": 5, "run_id": str(uuid.uuid4()), "tenant_id": tenant, "username": owner["username"], "role": "admin", "evaluation_id": base_evaluation, "suite_sha256": digest(validate_suite_manifest(suite)), "database_url": os.getenv("HARNESS_JOB_DATABASE_URL", database_url), "model_id": "/app/data/models/TinyLlama", "cases": suite["cases"], "max_new_tokens": 32}
    base_key = f"{prefix}/jobs/base-evaluation.json"
    base_hash = upload(store, base_key, json.dumps(base_input, sort_keys=True).encode())
    _, base_result = asyncio.run(run_job(runtime, owner, tool="h5_model_evaluate", input_key=base_key, input_sha256=base_hash, scope=f"evaluation:{base_evaluation}", extra={"evaluation_id": base_evaluation}))
    if json.loads(base_result)["tool_result"]["campaign_state"] != "passed":
        raise RuntimeError("rehearsal_base_evaluation_failed")

    annotation_items = []
    dataset_lines = []
    for index, (trial, trial_id) in enumerate(trials[:2]):
        sample = {"instruction": "Give a brief acknowledgement.", "input": "", "output": "Acknowledged."}
        body = json.dumps(sample, sort_keys=True).encode()
        content_key = f"{prefix}/annotations/{index}.json"
        content_sha256 = upload(store, content_key, body)
        annotation_id = evaluations.create_annotation(owner, run_id=trial["run_id"], trial_id=trial_id, kind="human_review", label={"simulation": True, "label": "approved"}, content_key=content_key, content_sha256=content_sha256)
        evaluations.review_annotation(reviewer, annotation_id, status="approved", training_allowed=True, training_purpose="deployment_model_improvement", permission_version="simulation-v1")
        annotation_items.append({"item_id": f"simulation-{index}", "split": "train" if index == 0 else "validation", "source_type": "trajectory_annotation", "source_id": annotation_id, "source_sha256": content_sha256, "source_acl_digest": digest(tenant), "training_allowed": True, "training_purpose": "deployment_model_improvement", "training_permission_version": "simulation-v1", "transform_digest": digest(sample)})
        dataset_lines.append(json.dumps(sample))
    dataset_body = ("\n".join(dataset_lines) + "\n").encode()
    dataset_key = f"{prefix}/training/dataset.jsonl"
    dataset_sha256 = upload(store, dataset_key, dataset_body)
    snapshot_id = evaluations.create_snapshot(owner, annotation_items=annotation_items, dataset_key=dataset_key, dataset_sha256=dataset_sha256, dataset_size=len(dataset_body), base_model_digest=base_digest, policy_version="h5-simulation-v1")
    evaluations.approve_snapshot(reviewer, snapshot_id)

    training_input = {"harness_version": 5, "run_id": str(uuid.uuid4()), "tenant_id": tenant, "username": owner["username"], "role": "admin", "snapshot_id": snapshot_id, "snapshot_state": "approved", "dataset_key": dataset_key, "dataset_sha256": dataset_sha256, "base_model_digest": base_digest, "tokenizer_digest": tokenizer_digest, "model_id": "/app/data/models/TinyLlama", "database_url": os.getenv("HARNESS_JOB_DATABASE_URL", database_url), "base_evaluation_id": base_evaluation, "base_evaluation_passed": True, "output_prefix": f"{prefix}/adapters", "environment": {"simulation": True}}
    training_key = f"{prefix}/jobs/lora-train.json"
    training_hash = upload(store, training_key, json.dumps(training_input, sort_keys=True).encode())
    _, training_result = asyncio.run(run_job(runtime, owner, tool="h5_train_lora", input_key=training_key, input_sha256=training_hash, scope=f"raw:{training_key}"))
    adapter_id = json.loads(training_result)["tool_result"]["output"]["adapter_id"]

    candidate_evaluation = evaluations.create_campaign(owner, suite, subject_type="adapter", subject_ref=adapter_id, required_trials=3)
    for number in range(1, 4):
        trial = asyncio.run(run_capture(runtime, owner, f"simulation:{tenant}:trial", number + 10))
        trial_id = evaluations.register_trial(owner, candidate_evaluation, trial, case_id="basic-generation", trial_no=number, fingerprint={"simulation": True, "adapter_id": adapter_id})
        evaluations.finish_trial(owner, trial_id, {"state": "succeeded", "metrics": {"simulation": True}})
    candidate_input = {**base_input, "run_id": str(uuid.uuid4()), "evaluation_id": candidate_evaluation, "use_adapter": True, "adapter_id": adapter_id, "baseline_evaluation_id": base_evaluation}
    candidate_key = f"{prefix}/jobs/adapter-evaluation.json"
    candidate_hash = upload(store, candidate_key, json.dumps(candidate_input, sort_keys=True).encode())
    _, candidate_result = asyncio.run(run_job(runtime, owner, tool="h5_model_evaluate", input_key=candidate_key, input_sha256=candidate_hash, scope=f"evaluation:{candidate_evaluation}", extra={"evaluation_id": candidate_evaluation}))
    if json.loads(candidate_result)["tool_result"]["campaign_state"] != "passed":
        raise RuntimeError("rehearsal_adapter_evaluation_failed")
    evaluations.verify_adapter(reviewer, adapter_id, candidate_evaluation)

    governance = ReleaseGovernance(database_url)
    manifest = {"harness_version": 5, "code_version": "h5-simulation-v1", "adapter_id": adapter_id, "evaluation_id": candidate_evaluation, "training_snapshot_id": snapshot_id, "evaluation": {"passed": True, "hard_gates_passed": True}, "rollback_to": "base", "guardrails": {"max_error_rate": 0.01, "max_p95_ms": 1000, "min_samples": 3, "window_seconds": 1}, "release_scope": "single_tenant_lora", "approvals": {"candidate": owner["username"], "promote": promoter["username"]}, "policy_version": "h5-simulation-v1", "simulation": True}
    rollback_release = governance.create_candidate(owner, manifest)
    governance.advance(rollback_release, "shadow", owner)
    governance.advance(rollback_release, "canary", owner)
    if governance.observe(rollback_release, {"sample_count": 3, "window_seconds": 1, "security_passed": True, "window_complete": True, "error_rate": 1.0, "p95_ms": 1}, promoter) != "rolled_back":
        raise RuntimeError("rehearsal_rollback_not_triggered")
    promoted_release = governance.create_candidate(owner, manifest)
    governance.advance(promoted_release, "shadow", owner)
    governance.advance(promoted_release, "canary", owner)
    if governance.observe(promoted_release, {"sample_count": 3, "window_seconds": 1, "security_passed": True, "window_complete": True, "error_rate": 0.0, "p95_ms": 1}, promoter) != "promoted":
        raise RuntimeError("rehearsal_promotion_failed")
    report = {"kind": "H5_RELEASE_REHEARSAL", "classification": "SIMULATION", "simulation": True, "tenant_id": tenant, "base_evaluation_id": base_evaluation, "candidate_evaluation_id": candidate_evaluation, "snapshot_id": snapshot_id, "adapter_id": adapter_id, "rollback_release_id": rollback_release, "promoted_release_id": promoted_release, "limitations": ["synthetic data", "empty-substring evaluator assertion", "not H5 final evidence"]}
    report_key = f"{prefix}/report.json"
    report["report_key"] = report_key
    report["report_sha256"] = upload(store, report_key, json.dumps(report, ensure_ascii=False, sort_keys=True).encode())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
