"""Run the dependency-free Phase 1 runtime acceptance baseline."""

# ruff: noqa: E402, I001

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.agent_runtime import AgentRuntime
from src.core.tool_contracts import ToolRegistry, ToolSpec
from src.core.verifiers import VerificationResult, VerifierSpec, default_verifiers


IDENTITY = {"username": "baseline_user", "tenant_id": "baseline", "role": "admin"}


def make_runtime(database: str, attempts: dict[str, int]) -> AgentRuntime:
    tools = ToolRegistry()
    tools.register(ToolSpec(name="read", handler=lambda args: {"value": args["value"]}))
    tools.register(
        ToolSpec(
            name="publish",
            handler=lambda args: {"published": args["name"]},
            requires_approval=True,
            idempotent=True,
            roles=frozenset({"admin"}),
        )
    )

    def transient(_args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary failure")
        return {"recovered": True}

    tools.register(ToolSpec(name="transient", handler=transient))
    verifiers = default_verifiers()
    verifiers.register(VerifierSpec("contract", 1, lambda *_: VerificationResult("passed")))
    return AgentRuntime(database, tools, verifiers)


async def evaluate() -> list[dict[str, str]]:
    attempts = {"count": 0}
    database = os.environ.get("DATABASE_URL", "")
    if not database:
        raise RuntimeError("DATABASE_URL is required for Phase 1 evaluation")
    runtime = make_runtime(database, attempts)
    results = []

    task = runtime.create_task(IDENTITY, "answer", [{"tool": "read", "arguments": {"value": "ok"}}])
    assert (await runtime.run(task["task_id"], IDENTITY))["state"] == "succeeded"
    results.append({"name": "read_task", "status": "passed"})

    task = runtime.create_task(
        IDENTITY,
        "replan",
        [
            {
                "tool": "read",
                "arguments": {"value": "one"},
                "scope_refs": [],
                "verifier_refs": ["contract"],
            },
            {"tool": "read", "arguments": {"value": "two"}, "scope_refs": []},
        ],
        max_steps=2,
        execution_mode="strict",
        task_spec={
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
            "limits": {"max_steps": 2, "deadline_seconds": 60},
        },
    )
    assert (await runtime.run(task["task_id"], IDENTITY))["current_step"] == 2
    results.append({"name": "multi_step_replan", "status": "passed"})

    task = runtime.create_task(
        IDENTITY,
        "publish",
        [{"tool": "publish", "arguments": {"name": "v1"}, "idempotency_key": "v1"}],
    )
    assert (await runtime.run(task["task_id"], IDENTITY))["state"] == "waiting_approval"
    runtime = make_runtime(database, attempts)
    runtime.approve(task["task_id"], IDENTITY, True)
    assert (await runtime.run(task["task_id"], IDENTITY))["state"] == "succeeded"
    results.append({"name": "approval_checkpoint_recovery", "status": "passed"})

    task = runtime.create_task(IDENTITY, "retry", [{"tool": "transient"}])
    assert (await runtime.run(task["task_id"], IDENTITY))["state"] == "failed"
    runtime.retry(task["task_id"], IDENTITY)
    assert (await runtime.run(task["task_id"], IDENTITY))["state"] == "succeeded"
    results.append({"name": "temporary_failure_retry", "status": "passed"})

    task = runtime.create_task(IDENTITY, "private", [{"tool": "read", "arguments": {"value": "x"}}])
    try:
        runtime.get_task(task["task_id"], {**IDENTITY, "tenant_id": "other"})
    except PermissionError:
        results.append({"name": "tenant_isolation", "status": "passed"})
    else:
        raise AssertionError("cross-tenant task access was allowed")
    return results


if __name__ == "__main__":
    outcome = asyncio.run(evaluate())
    print(
        json.dumps(
            {"baseline": "phase1_runtime_control_plane", "tasks": outcome}, ensure_ascii=False
        )
    )
