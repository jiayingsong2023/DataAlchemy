import asyncio
import os
import threading
import uuid

import pytest

from src.core.agent_runtime import AgentRuntime
from src.core.tool_contracts import ToolRegistry, ToolSpec
from src.core.verifiers import VerificationResult, VerifierRegistry, VerifierSpec, default_verifiers


def identity(username="alice", tenant_id="acme", role="user"):
    return {"username": username, "tenant_id": tenant_id, "role": role}


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def runtime():
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="echo",
            handler=lambda args: {"answer": args["text"]},
            schema={
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    )
    verifiers = default_verifiers()
    verifiers.register(VerifierSpec("contract", 1, lambda *_: VerificationResult("passed")))
    return AgentRuntime(os.environ["TEST_DATABASE_URL"], tools, verifiers), tools


def strict_spec(*, sources=None, max_steps=2):
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
        "data_scope": {"source_refs": sources or []},
        "limits": {"max_steps": max_steps, "deadline_seconds": 60},
    }


@pytest.mark.asyncio
async def test_runtime_records_plan_observation_and_checkpoint():
    runtime_one, _ = runtime()
    task = runtime_one.create_task(
        identity(), "answer", [{"tool": "echo", "arguments": {"text": "hello"}}]
    )

    completed = await runtime_one.run(task["task_id"], identity())
    restarted, _ = runtime()

    assert completed["state"] == "succeeded"
    assert restarted.get_task(task["task_id"], identity())["current_step"] == 1
    assert [event["event_type"] for event in restarted.events(task["task_id"], identity())] == [
        "planned",
        "lease_acquired",
        "started",
        "tool_attempt_started",
        "observed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_high_risk_tool_requires_recorded_approval_and_is_idempotent():
    calls = []
    idempotency_key = f"release-{uuid.uuid4()}"
    runtime_one, tools = runtime()
    tools.register(
        ToolSpec(
            name="publish",
            handler=lambda args: calls.append(args) or {"published": args["name"]},
            schema={"type": "object", "required": ["name"]},
            requires_approval=True,
            idempotent=True,
        )
    )
    task = runtime_one.create_task(
        identity(),
        "publish",
        [{"tool": "publish", "arguments": {"name": "v1"}, "idempotency_key": idempotency_key}],
    )

    waiting = await runtime_one.run(task["task_id"], identity())
    rejected = runtime_one.approve(task["task_id"], identity(), approved=False)

    assert waiting["state"] == "waiting_approval"
    assert calls == []
    assert rejected["state"] == "cancelled"

    approved = runtime_one.create_task(
        identity(),
        "publish",
        [{"tool": "publish", "arguments": {"name": "v1"}, "idempotency_key": idempotency_key}],
    )
    await runtime_one.run(approved["task_id"], identity())
    runtime_one.approve(approved["task_id"], identity(), approved=True)
    done = await runtime_one.run(approved["task_id"], identity())

    assert done["state"] == "succeeded"
    assert calls == [{"name": "v1"}]


@pytest.mark.asyncio
async def test_task_access_and_failed_tool_are_isolated():
    runtime_one, tools = runtime()
    tools.register(
        ToolSpec(
            name="broken", handler=lambda _args: (_ for _ in ()).throw(RuntimeError("offline"))
        )
    )
    task = runtime_one.create_task(identity(), "fail", [{"tool": "broken"}])

    with pytest.raises(PermissionError):
        runtime_one.get_task(task["task_id"], identity("bob"))
    with pytest.raises(PermissionError):
        runtime_one.events(task["task_id"], identity("alice", "other"))

    failed = await runtime_one.run(task["task_id"], identity())
    assert failed["state"] == "failed"
    assert "offline" in failed["finish_reason"]


@pytest.mark.asyncio
async def test_tool_authorization_uses_the_callers_current_role():
    runtime_one, tools = runtime()
    tools.register(
        ToolSpec(name="admin_only", handler=lambda _args: {}, roles=frozenset({"admin"}))
    )
    task = runtime_one.create_task(identity(role="admin"), "admin action", [{"tool": "admin_only"}])

    denied = await runtime_one.run(task["task_id"], identity(role="user"))

    assert denied["state"] == "failed"
    assert "cannot call" in denied["finish_reason"]


@pytest.mark.asyncio
async def test_failed_task_can_retry_from_its_persisted_checkpoint():
    attempts = 0
    runtime_one, tools = runtime()

    def eventually_available(_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary outage")
        return {"status": "ok"}

    tools.register(ToolSpec(name="transient", handler=eventually_available))
    task = runtime_one.create_task(identity(), "retry", [{"tool": "transient"}])

    assert (await runtime_one.run(task["task_id"], identity()))["state"] == "failed"
    runtime_one.retry(task["task_id"], identity())

    assert (await runtime_one.run(task["task_id"], identity()))["state"] == "succeeded"
    assert attempts == 2


@pytest.mark.asyncio
async def test_gateway_redacts_events_limits_calls_and_retries_idempotent_tools():
    runtime_one, tools = runtime()
    attempts = 0

    def flaky(_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary outage")
        return {"token": "secret", "status": "ok"}

    tools.register(
        ToolSpec(
            name="sync",
            handler=flaky,
            idempotent=True,
            max_retries=1,
            max_calls_per_minute=1,
            sensitive_fields=frozenset({"token"}),
        )
    )
    first = runtime_one.create_task(
        identity(), "sync", [{"tool": "sync", "idempotency_key": str(uuid.uuid4())}]
    )
    assert (await runtime_one.run(first["task_id"], identity()))["state"] == "succeeded"
    observed = [
        event
        for event in runtime_one.events(first["task_id"], identity())
        if event["event_type"] == "observed"
    ]
    assert "result" not in observed[0]["payload"]
    assert observed[0]["payload"]["result_digest"]
    assert attempts == 2

    second = runtime_one.create_task(
        identity(), "again", [{"tool": "sync", "idempotency_key": str(uuid.uuid4())}]
    )
    limited = await runtime_one.run(second["task_id"], identity())
    assert limited["state"] == "failed"
    assert "rate limit" in limited["finish_reason"]


@pytest.mark.asyncio
async def test_strict_task_freezes_contract_and_waits_for_verification():
    runtime_one, tools = runtime()
    tools.register(ToolSpec(name="second", handler=lambda _args: {"status": "ok"}))
    task = runtime_one.create_task(
        identity(),
        "two steps",
        [
            {
                "tool": "echo",
                "arguments": {"text": "one"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            },
            {"tool": "second", "arguments": {}, "scope_refs": []},
        ],
        max_steps=2,
        execution_mode="strict",
        task_spec=strict_spec(sources=["source:a"]),
    )

    result = await runtime_one.run(task["task_id"], identity())

    assert result["state"] == "succeeded"
    assert result["task_spec"]["execution_mode"] == "strict"
    assert result["run_id"]
    assert len({step["step_id"] for step in result["plan"]}) == 2
    assert all(
        step["idempotency_key"] == f"{result['run_id']}:{step['step_id']}"
        for step in result["plan"]
    )
    assert result["plan_version"] == 1


@pytest.mark.asyncio
async def test_failed_verification_stops_before_the_checkpoint():
    tools = ToolRegistry()
    calls = []
    tools.register(
        ToolSpec(name="echo", handler=lambda args: calls.append(args) or {"answer": args["text"]})
    )
    verifiers = VerifierRegistry()
    verifiers.register(
        VerifierSpec("contract", 1, lambda *_: VerificationResult("failed", error_code="no"))
    )
    runtime_one = AgentRuntime(os.environ["TEST_DATABASE_URL"], tools, verifiers)
    task = runtime_one.create_task(
        identity(),
        "verify",
        [
            {
                "tool": "echo",
                "arguments": {"text": "one"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(max_steps=1),
    )

    result = await runtime_one.run(task["task_id"], identity())

    assert result["state"] == "verification_failed"
    assert result["current_step"] == 0
    assert calls == [{"text": "one"}]
    assert runtime_one.verifications(task["task_id"], identity())[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_blocked_verification_reuses_the_immutable_tool_result():
    tools = ToolRegistry()
    calls = []
    tools.register(
        ToolSpec(name="echo", handler=lambda args: calls.append(args) or {"answer": args["text"]})
    )
    attempts = 0

    def verifier(*_):
        nonlocal attempts
        attempts += 1
        return VerificationResult("blocked" if attempts == 1 else "passed")

    verifiers = VerifierRegistry()
    verifiers.register(VerifierSpec("contract", 1, verifier))
    runtime_one = AgentRuntime(os.environ["TEST_DATABASE_URL"], tools, verifiers)
    task = runtime_one.create_task(
        identity(),
        "retry verify",
        [
            {
                "tool": "echo",
                "arguments": {"text": "one"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            }
        ],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(max_steps=1),
    )

    blocked = await runtime_one.run(task["task_id"], identity())
    runtime_one.retry_verification(task["task_id"], identity(), blocked["version"])
    done = await runtime_one.run(task["task_id"], identity())

    assert blocked["state"] == "verification_blocked"
    assert done["state"] == "succeeded"
    assert calls == [{"text": "one"}]
    assert [row["attempt"] for row in runtime_one.verifications(task["task_id"], identity())] == [
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_legacy_rejects_side_effecting_tool_before_execution():
    runtime_one, tools = runtime()
    tools.register(
        ToolSpec(name="write", handler=lambda _args: {}, idempotent=True, side_effecting=True)
    )

    with pytest.raises(ValueError, match="Legacy tasks cannot call side-effecting"):
        runtime_one.create_task(identity(), "write", [{"tool": "write"}])


@pytest.mark.asyncio
async def test_side_effect_failure_requires_reconciliation_without_retry():
    calls = []
    runtime_one, tools = runtime()
    tools.register(
        ToolSpec(
            name="write",
            handler=lambda _args: calls.append("called")
            or (_ for _ in ()).throw(RuntimeError("connection lost")),
            idempotent=True,
            side_effecting=True,
        )
    )
    task = runtime_one.create_task(
        identity(),
        "write",
        [{"tool": "write", "scope_refs": [], "verifier_refs": ["contract"]}],
        max_steps=1,
        execution_mode="strict",
        task_spec=strict_spec(max_steps=1),
    )

    result = await runtime_one.run(task["task_id"], identity())

    assert result["state"] == "reconciliation_required"
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_replan_preserves_completed_prefix_and_requires_resume():
    runtime_one, tools = runtime()
    tools.register(ToolSpec(name="second", handler=lambda _args: {"status": "ok"}))
    task = runtime_one.create_task(
        identity(),
        "replan",
        [
            {
                "tool": "echo",
                "arguments": {"text": "one"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            },
            {"tool": "second", "scope_refs": []},
        ],
        max_steps=2,
        execution_mode="strict",
        task_spec=strict_spec(max_steps=2),
    )
    paused = runtime_one.pause(task["task_id"], identity(), task["version"])
    replanned = runtime_one.replan(
        task["task_id"],
        identity(),
        [{"tool": "second", "scope_refs": [], "verifier_refs": ["contract"]}],
        "change approach",
        paused["version"],
    )

    assert replanned["state"] == "paused"
    assert replanned["plan_version"] == 2
    assert replanned["plan"][0]["step_id"] != task["plan"][0]["step_id"]
    assert replanned["plan"][0]["tool"] == "second"
    resumed = runtime_one.resume(task["task_id"], identity(), replanned["version"])
    assert (await runtime_one.run(task["task_id"], identity()))["state"] == "succeeded"
    assert resumed["state"] == "created"


@pytest.mark.asyncio
async def test_pause_and_cancel_use_safe_stopped_states():
    runtime_one, _ = runtime()
    task = runtime_one.create_task(
        identity(), "stop", [{"tool": "echo", "arguments": {"text": "x"}}]
    )
    paused = runtime_one.pause(task["task_id"], identity(), task["version"])
    assert paused["state"] == "paused"
    cancelled = runtime_one.cancel(task["task_id"], identity(), paused["version"])
    assert cancelled["state"] == "cancelled"


@pytest.mark.asyncio
async def test_only_one_worker_can_hold_a_task_lease():
    calls = []
    entered = threading.Event()
    release = threading.Event()
    runtime_one, tools = runtime()

    def slow(_args):
        calls.append("called")
        entered.set()
        assert release.wait(timeout=3)
        return {"status": "ok"}

    tools.register(ToolSpec(name="slow", handler=slow))
    task = runtime_one.create_task(identity(), "slow", [{"tool": "slow"}])
    first = asyncio.create_task(
        runtime_one.run(task["task_id"], identity(), worker_id="worker-one")
    )
    assert await asyncio.to_thread(entered.wait, 3)
    with pytest.raises(RuntimeError, match="already running"):
        await runtime_one.run(task["task_id"], identity(), worker_id="worker-two")
    release.set()
    assert (await first)["state"] == "succeeded"
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_pause_requested_while_a_tool_runs_waits_for_the_safe_point():
    entered = threading.Event()
    release = threading.Event()
    runtime_one, tools = runtime()

    def slow(_args):
        entered.set()
        assert release.wait(timeout=3)
        return {"status": "ok"}

    tools.register(ToolSpec(name="slow", handler=slow))
    task = runtime_one.create_task(identity(), "pause", [{"tool": "slow"}])
    runner = asyncio.create_task(runtime_one.run(task["task_id"], identity()))
    assert await asyncio.to_thread(entered.wait, 3)
    running = runtime_one.get_task(task["task_id"], identity())
    pausing = runtime_one.pause(task["task_id"], identity(), running["version"])
    assert pausing["state"] == "pausing"
    release.set()
    assert (await runner)["state"] == "paused"
