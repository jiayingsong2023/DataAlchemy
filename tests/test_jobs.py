import json
import os

import pytest

from src.core.agent_runtime import AgentRuntime
from src.core.evidence import ObjectNotFound
from src.core.jobs import JobObservation, JobService
from src.core.tool_contracts import ToolRegistry, ToolSpec
from src.core.verifiers import VerificationResult, VerifierRegistry, VerifierSpec

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity():
    return {"username": "alice", "tenant_id": "acme", "role": "admin"}


class FakeBackend:
    def __init__(self, result=None, state="succeeded"):
        self.result = result
        self.state = state
        self.calls = []

    def submit(self, job):
        self.calls.append(("submit", job["job_id"]))
        return JobObservation("running", "uid-1")

    def observe(self, job):
        self.calls.append(("observe", job["job_id"]))
        return JobObservation(
            self.state, "uid-1", self.result, "job_failed" if self.state == "failed" else None
        )

    def cancel(self, job):
        self.calls.append(("cancel", job["job_id"]))
        return JobObservation("cancelled")


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, body):
        self.objects[key] = body

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def copy(self, source, target):
        self.objects[target] = self.objects[source]

    def delete(self, key):
        self.objects.pop(key, None)


def runtime(backend, store=None):
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="spark_rough_clean",
            handler=lambda _arguments: (_ for _ in ()).throw(
                AssertionError("job handler must not run inline")
            ),
            schema={"type": "object", "required": ["input_key", "input_sha256"]},
            roles=frozenset({"admin"}),
            requires_approval=True,
            idempotent=True,
            side_effecting=True,
            execution="kubernetes_job",
            job_kind="spark_rough_clean",
            scope_resolver=lambda arguments, _identity: [f"raw:{arguments['input_key']}"],
            expected_artifacts=frozenset({("minio", "cleaned_corpus")}),
            result_sensitivity={"*": "internal"},
        )
    )
    verifiers = VerifierRegistry()
    verifiers.register(VerifierSpec("contract", 1, lambda *_: VerificationResult("passed")))
    return AgentRuntime(
        os.environ["TEST_DATABASE_URL"],
        tools,
        verifiers,
        jobs=JobService(os.environ["TEST_DATABASE_URL"], backend, store),
    )


def spec():
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
        "data_scope": {"source_refs": ["raw:s3a://data-alchemy/raw/acme"]},
        "limits": {"max_steps": 1, "deadline_seconds": 60},
    }


@pytest.mark.asyncio
async def test_job_handle_is_observed_before_its_result_can_checkpoint():
    result = {
        "output": {"cleaned": 1},
        "observed_scope": ["raw:s3a://data-alchemy/raw/acme"],
        "artifacts": [
            {
                "store": "minio",
                "kind": "cleaned_corpus",
                "id": "processed/acme.jsonl",
                "sha256": "a" * 64,
            }
        ],
    }
    backend = FakeBackend(result)
    runtime_one = runtime(backend)
    task = runtime_one.create_task(
        identity(),
        "rough clean",
        [
            {
                "tool": "spark_rough_clean",
                "arguments": {"input_key": "s3a://data-alchemy/raw/acme", "input_sha256": "b" * 64},
                "scope_refs": ["raw:s3a://data-alchemy/raw/acme"],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=spec(),
    )
    approval = await runtime_one.run(task["task_id"], identity())
    runtime_one.approve(task["task_id"], identity(), True, approval["version"])
    waiting = await runtime_one.run(task["task_id"], identity())
    still_waiting = await runtime_one.reconcile_job(task["task_id"], identity(), waiting["version"])
    completed = await runtime_one.reconcile_job(
        task["task_id"], identity(), still_waiting["version"]
    )

    assert waiting["state"] == "waiting_job"
    assert still_waiting["state"] == "waiting_job"
    assert completed["state"] == "succeeded"
    assert [kind for kind, _ in backend.calls] == ["submit", "observe"]


@pytest.mark.asyncio
async def test_failed_job_never_advances_its_checkpoint():
    runtime_one = runtime(FakeBackend(state="failed"))
    task = runtime_one.create_task(
        identity(),
        "rough clean fails",
        [
            {
                "tool": "spark_rough_clean",
                "arguments": {"input_key": "s3a://data-alchemy/raw/acme", "input_sha256": "b" * 64},
                "scope_refs": ["raw:s3a://data-alchemy/raw/acme"],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=spec(),
    )
    approval = await runtime_one.run(task["task_id"], identity())
    runtime_one.approve(task["task_id"], identity(), True, approval["version"])
    waiting = await runtime_one.run(task["task_id"], identity())
    failed = await runtime_one.reconcile_job(task["task_id"], identity(), waiting["version"])
    failed = await runtime_one.reconcile_job(task["task_id"], identity(), failed["version"])

    assert failed["state"] == "failed"
    assert failed["current_step"] == 0


@pytest.mark.asyncio
async def test_completed_kubernetes_job_requires_a_matching_minio_result_manifest():
    store = MemoryStore()
    runtime_one = runtime(FakeBackend(), store)
    task = runtime_one.create_task(
        identity(),
        "manifest",
        [
            {
                "tool": "spark_rough_clean",
                "arguments": {"input_key": "s3a://data-alchemy/raw/acme", "input_sha256": "b" * 64},
                "scope_refs": ["raw:s3a://data-alchemy/raw/acme"],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=spec(),
    )
    approval = await runtime_one.run(task["task_id"], identity())
    runtime_one.approve(task["task_id"], identity(), True, approval["version"])
    waiting = await runtime_one.run(task["task_id"], identity())
    submitted = await runtime_one.reconcile_job(task["task_id"], identity(), waiting["version"])
    job = runtime_one.jobs.for_task(submitted, identity())
    store.put(
        job["result_key"],
        json.dumps(
            {
                "job_id": job["job_id"],
                "input_key": job["input_key"],
                "input_sha256": job["input_sha256"],
                "tool_result": {
                    "output": {"cleaned": 1},
                    "observed_scope": ["raw:s3a://data-alchemy/raw/acme"],
                    "artifacts": [
                        {
                            "store": "minio",
                            "kind": "cleaned_corpus",
                            "id": "processed/acme.jsonl",
                            "sha256": "a" * 64,
                        }
                    ],
                },
            }
        ).encode(),
    )

    completed = await runtime_one.reconcile_job(task["task_id"], identity(), submitted["version"])

    assert completed["state"] == "succeeded"
    assert runtime_one.jobs.get(job["job_id"], identity())["result_sha256"]


@pytest.mark.asyncio
async def test_cancel_waits_for_the_kubernetes_job_to_confirm_termination():
    backend = FakeBackend()
    runtime_one = runtime(backend)
    task = runtime_one.create_task(
        identity(),
        "cancel job",
        [
            {
                "tool": "spark_rough_clean",
                "arguments": {"input_key": "s3a://data-alchemy/raw/acme", "input_sha256": "b" * 64},
                "scope_refs": ["raw:s3a://data-alchemy/raw/acme"],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=spec(),
    )
    approval = await runtime_one.run(task["task_id"], identity())
    runtime_one.approve(task["task_id"], identity(), True, approval["version"])
    waiting = await runtime_one.run(task["task_id"], identity())
    cancelling = runtime_one.cancel(task["task_id"], identity(), waiting["version"])
    cancelled = await runtime_one.reconcile_job(task["task_id"], identity(), cancelling["version"])

    assert cancelling["state"] == "cancelling"
    assert cancelled["state"] == "cancelled"
    assert [kind for kind, _ in backend.calls] == ["cancel"]
