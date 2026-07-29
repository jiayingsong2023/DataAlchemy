import pytest

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec


def identity(username="alice", tenant_id="acme", role="user"):
    return {"username": username, "tenant_id": tenant_id, "role": role}


def runtime(tmp_path):
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
    return AgentRuntime(str(tmp_path / "runtime.db"), tools), tools


@pytest.mark.asyncio
async def test_runtime_records_plan_observation_and_checkpoint(tmp_path):
    runtime_one, _ = runtime(tmp_path)
    task = runtime_one.create_task(
        identity(), "answer", [{"tool": "echo", "arguments": {"text": "hello"}}]
    )

    completed = await runtime_one.run(task["task_id"], identity())
    restarted, _ = runtime(tmp_path)

    assert completed["state"] == "succeeded"
    assert restarted.get_task(task["task_id"], identity())["current_step"] == 1
    assert [event["event_type"] for event in restarted.events(task["task_id"], identity())] == [
        "planned",
        "started",
        "observed",
        "replanned",
        "completed",
    ]


@pytest.mark.asyncio
async def test_high_risk_tool_requires_recorded_approval_and_is_idempotent(tmp_path):
    calls = []
    runtime_one, tools = runtime(tmp_path)
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
        [{"tool": "publish", "arguments": {"name": "v1"}, "idempotency_key": "release-v1"}],
    )

    waiting = await runtime_one.run(task["task_id"], identity())
    rejected = runtime_one.approve(task["task_id"], identity(), approved=False)

    assert waiting["state"] == "waiting_approval"
    assert calls == []
    assert rejected["state"] == "cancelled"

    approved = runtime_one.create_task(
        identity(),
        "publish",
        [{"tool": "publish", "arguments": {"name": "v1"}, "idempotency_key": "release-v1"}],
    )
    await runtime_one.run(approved["task_id"], identity())
    runtime_one.approve(approved["task_id"], identity(), approved=True)
    done = await runtime_one.run(approved["task_id"], identity())

    assert done["state"] == "succeeded"
    assert calls == [{"name": "v1"}]


@pytest.mark.asyncio
async def test_task_access_and_failed_tool_are_isolated(tmp_path):
    runtime_one, tools = runtime(tmp_path)
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
async def test_tool_authorization_uses_the_callers_current_role(tmp_path):
    runtime_one, tools = runtime(tmp_path)
    tools.register(
        ToolSpec(name="admin_only", handler=lambda _args: {}, roles=frozenset({"admin"}))
    )
    task = runtime_one.create_task(identity(role="admin"), "admin action", [{"tool": "admin_only"}])

    denied = await runtime_one.run(task["task_id"], identity(role="user"))

    assert denied["state"] == "failed"
    assert "cannot call" in denied["finish_reason"]


@pytest.mark.asyncio
async def test_failed_task_can_retry_from_its_persisted_checkpoint(tmp_path):
    attempts = 0
    runtime_one, tools = runtime(tmp_path)

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
