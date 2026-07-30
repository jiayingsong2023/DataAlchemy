"""Exercise the durable task gateway against the Phase 3 engineering task set."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec


async def main() -> None:
    cases = json.loads(
        (Path(__file__).resolve().parents[1] / "eval/phase3_pilot_tasks.json").read_text()
    )["cases"]
    identity = {
        "tenant_id": f"phase3-eval-{uuid.uuid4()}",
        "username": "evaluator",
        "role": "admin",
    }
    tools = ToolRegistry()
    tools.register(ToolSpec(name="read", handler=lambda arguments: {"evidence": arguments["goal"]}))
    runtime = AgentRuntime(os.environ["DATABASE_URL"], tools)
    completed = 0
    for goal in cases:
        task = runtime.create_task(identity, goal, [{"tool": "read", "arguments": {"goal": goal}}])
        completed += (await runtime.run(task["task_id"], identity))["state"] == "succeeded"
    result = {
        "suite": "phase3_engineering_acceptance",
        "cases": len(cases),
        "task_success_rate": completed / len(cases),
    }
    print(json.dumps(result))
    assert result["task_success_rate"] == 1.0


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
