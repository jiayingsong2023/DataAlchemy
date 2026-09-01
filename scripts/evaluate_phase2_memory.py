"""Run the deterministic Phase 2 memory governance acceptance suite."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.agent_runtime import AgentRuntime
from src.core.tool_contracts import ToolRegistry, ToolSpec
from src.memory.orchestrator import MemoryOrchestrator
from src.rag.vector_store import VectorStore


class CaseEmbedding:
    def encode(self, values, convert_to_numpy=True):
        vectors = []
        for value in values:
            index = int(str(value).rsplit("-", 1)[-1]) % 512
            vector = np.zeros(512)
            vector[index] = 1
            vectors.append(vector)
        return np.array(vectors)


def identity(tenant_id: str, username: str = "phase2-evaluator"):
    return {"tenant_id": tenant_id, "username": username, "role": "admin"}


def main() -> None:
    database = os.environ["DATABASE_URL"]
    case_file = Path(__file__).resolve().parents[1] / "eval/phase2_memory_cases.json"
    cases = json.loads(case_file.read_text())["cases"]
    runtime = AgentRuntime(database, ToolRegistry())
    runtime.tools.register(ToolSpec(name="noop", handler=lambda _: {}))
    store = VectorStore(database_url=database)
    store.model = CaseEmbedding()
    memories = MemoryOrchestrator(database, store, None)
    approved = unapproved = cross_tenant = 0
    for number, _case in enumerate(cases, 1):
        owner = identity(f"phase2-eval-{uuid.uuid4()}")
        task = runtime.create_task(owner, "evidence", [{"tool": "noop"}])
        event_id = runtime.events(task["task_id"], owner)[0]["event_id"]
        content = f"case-{number}"
        memory_id = memories.create_candidate(owner, "profile", content, event_id)
        unapproved += bool(memories.retrieve(content, owner, 1))
        memories.approve(memory_id, owner)
        approved += memories.retrieve(content, owner, 1)[0]["memory_id"] == memory_id
        cross_tenant += bool(memories.retrieve(content, identity(f"other-{uuid.uuid4()}"), 1))
    result = {
        "suite": "phase2_memory_governance",
        "cases": len(cases),
        "approved_recall_at_1": approved / len(cases),
        "unapproved_recall": unapproved / len(cases),
        "cross_tenant_recall": cross_tenant / len(cases),
    }
    print(json.dumps(result))
    assert result["approved_recall_at_1"] == 1.0
    assert result["unapproved_recall"] == result["cross_tenant_recall"] == 0.0


if __name__ == "__main__":
    main()
