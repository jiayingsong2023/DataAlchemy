import os

import pytest

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec
from src.core.evidence import EvidenceService, ObjectNotFound
from src.core.verifiers import VerificationResult, VerifierRegistry, VerifierSpec

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity(username="alice", tenant_id="acme", role="user"):
    return {"username": username, "tenant_id": tenant_id, "role": role}


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.tamper_copy = False

    def put(self, key, body):
        self.objects[key] = body

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def copy(self, source, target):
        self.objects[target] = b"tampered" if self.tamper_copy else self.objects[source]

    def delete(self, key):
        self.objects.pop(key, None)


def strict_spec():
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
        "data_scope": {"source_refs": []},
        "limits": {"max_steps": 1, "deadline_seconds": 60},
    }


def runtime(store):
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="echo",
            handler=lambda arguments: {"answer": arguments["text"], "secret": "never publish"},
            schema={"type": "object", "required": ["text"]},
            result_sensitivity={"answer": "public", "secret": "secret"},
        )
    )
    verifiers = VerifierRegistry()
    verifiers.register(VerifierSpec("contract", 1, lambda *_: VerificationResult("passed")))
    evidence = EvidenceService(os.environ["TEST_DATABASE_URL"], store, tools.sensitivity)
    return AgentRuntime(os.environ["TEST_DATABASE_URL"], tools, verifiers, evidence), evidence


@pytest.mark.asyncio
async def test_success_requires_a_published_redacted_manifest():
    runtime_one, evidence = runtime(MemoryStore())
    task = runtime_one.create_task(
        identity(),
        "evidence",
        [
            {
                "tool": "echo",
                "arguments": {"text": "hello"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(),
    )

    completed = await runtime_one.run(task["task_id"], identity())
    manifest = evidence.manifest(task["run_id"], identity())

    assert completed["state"] == "succeeded"
    assert manifest["run"]["outcome"] == "development_evidence"
    assert manifest["steps"][0]["tool_result"]["output"]["answer"] == "hello"
    assert "secret" not in manifest["steps"][0]["tool_result"]["output"]
    assert runtime_one.evidence_status(task["task_id"], identity())["state"] == "published"
    with pytest.raises(PermissionError):
        evidence.manifest(task["run_id"], identity("bob", "other"))


@pytest.mark.asyncio
async def test_tampered_final_object_stays_pending_until_reconciled():
    store = MemoryStore()
    store.tamper_copy = True
    runtime_one, evidence = runtime(store)
    task = runtime_one.create_task(
        identity(),
        "recover evidence",
        [
            {
                "tool": "echo",
                "arguments": {"text": "hello"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(),
    )

    pending = await runtime_one.run(task["task_id"], identity())
    store.tamper_copy = False
    completed = runtime_one.reconcile_evidence(task["task_id"], identity(), pending["version"])

    assert pending["state"] == "evidence_pending"
    assert runtime_one.evidence_status(task["task_id"], identity())["state"] == "published"
    assert completed["state"] == "succeeded"
    assert evidence.manifest(task["run_id"], identity())["run"]["run_id"] == task["run_id"]


@pytest.mark.asyncio
async def test_admin_delete_tombstones_the_manifest_without_reusing_its_index():
    store = MemoryStore()
    runtime_one, evidence = runtime(store)
    task = runtime_one.create_task(
        identity(role="admin"),
        "delete evidence",
        [
            {
                "tool": "echo",
                "arguments": {"text": "hello"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(),
    )
    await runtime_one.run(task["task_id"], identity(role="admin"))

    runtime_one.delete_evidence(task["task_id"], identity(role="admin"))

    assert (
        runtime_one.evidence_status(task["task_id"], identity(role="admin"))["state"] == "deleted"
    )
    with pytest.raises(PermissionError):
        evidence.manifest(task["run_id"], identity(role="admin"))
