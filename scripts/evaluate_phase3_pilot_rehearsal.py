"""Run an accelerated, two-tenant four-week Phase 3 pilot rehearsal."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec
from src.rag.vector_store import VectorStore


class Embeddings:
    def encode(self, values, convert_to_numpy=True):
        return [[0.1] * 512 for _ in values]


def identity(team: str) -> dict[str, str]:
    return {"tenant_id": f"rehearsal-{team}-{uuid.uuid4()}", "username": team, "role": "admin"}


async def main() -> None:
    database = os.environ["DATABASE_URL"]
    teams = [identity("team-a"), identity("team-b")]
    tools = ToolRegistry()
    attempts: dict[str, int] = {}

    def recover(arguments):
        attempts[arguments["case"]] = attempts.get(arguments["case"], 0) + 1
        if attempts[arguments["case"]] == 1:
            raise OSError("injected outage")
        return {"recovered": True}

    tools.register(ToolSpec(name="read", handler=lambda arguments: {"source": arguments["goal"]}))
    tools.register(
        ToolSpec(
            name="write",
            handler=lambda _: {"approved": True},
            requires_approval=True,
            idempotent=True,
        )
    )
    tools.register(
        ToolSpec(name="recover", handler=recover, schema={"type": "object", "required": ["case"]})
    )
    runtime = AgentRuntime(database, tools)
    store = VectorStore(database_url=database)
    store.model = Embeddings()
    completed = approvals = recoveries = audited = 0
    first_task = None
    for week in range(1, 5):
        for team in teams:
            source = f"rehearsal://{team['tenant_id']}/week-{week}"
            store.add_documents(
                [{"text": f"week {week} {team['tenant_id']}", "source": source}], team
            )
            assert store.search_text(team["tenant_id"], team)
            for number in range(10):
                task = runtime.create_task(
                    team,
                    f"week-{week}-task-{number}",
                    [{"tool": "read", "arguments": {"goal": source}}],
                )
                completed += (await runtime.run(task["task_id"], team))["state"] == "succeeded"
                first_task = first_task or task
            write = runtime.create_task(
                team,
                "approved sync",
                [{"tool": "write", "idempotency_key": f"{week}-{team['tenant_id']}"}],
            )
            assert (await runtime.run(write["task_id"], team))["state"] == "waiting_approval"
            runtime.approve(write["task_id"], team, approved=True)
            approvals += (await runtime.run(write["task_id"], team))["state"] == "succeeded"
            recovery = runtime.create_task(
                team, "recovery", [{"tool": "recover", "arguments": {"case": str(uuid.uuid4())}}]
            )
            assert (await runtime.run(recovery["task_id"], team))["state"] == "failed"
            runtime.retry(recovery["task_id"], team)
            recoveries += (await runtime.run(recovery["task_id"], team))["state"] == "succeeded"
            audited += len(runtime.events(write["task_id"], team)) >= 4
    assert first_task is not None
    try:
        runtime.get_task(first_task["task_id"], teams[1])
    except PermissionError:
        cross_tenant_task_visibility = 0
    else:
        cross_tenant_task_visibility = 1
    cross_tenant_source_visibility = len(store.search_text(teams[0]["tenant_id"], teams[1]))
    result = {
        "suite": "phase3_accelerated_pilot_rehearsal",
        "weeks": 4,
        "teams": 2,
        "tasks": completed,
        "approval_recovery_rate": (approvals + recoveries) / 16,
        "weekly_audits": audited,
        "cross_tenant_task_visibility": cross_tenant_task_visibility,
        "cross_tenant_source_visibility": cross_tenant_source_visibility,
    }
    print(json.dumps(result))
    assert completed == 80 and result["approval_recovery_rate"] == 1.0
    assert audited == 8 and cross_tenant_task_visibility == cross_tenant_source_visibility == 0


if __name__ == "__main__":
    asyncio.run(main())
