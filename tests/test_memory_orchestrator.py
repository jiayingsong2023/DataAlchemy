import os
import uuid
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.core.agent_runtime import AgentRuntime
from src.core.tool_contracts import ToolRegistry, ToolSpec
from src.memory.orchestrator import MemoryOrchestrator
from src.rag.vector_store import VectorStore

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


class FakeEmbeddingModel:
    def encode(self, values, convert_to_numpy=True):
        return np.array([[0.1] * 512 for _ in values])


def identity(username="alice", tenant_id="memory-acme", role="user"):
    return {"username": username, "tenant_id": tenant_id, "role": role}


@pytest.mark.asyncio
async def test_candidate_requires_approval_and_deletion_removes_retrieval():
    tools = ToolRegistry()
    tools.register(ToolSpec(name="echo", handler=lambda args: args))
    runtime = AgentRuntime(os.environ["TEST_DATABASE_URL"], tools)
    task = runtime.create_task(identity(), "source", [{"tool": "echo"}])
    event_id = runtime.events(task["task_id"], identity())[0]["event_id"]

    vector_store = VectorStore(database_url=os.environ["TEST_DATABASE_URL"])
    vector_store.model = FakeEmbeddingModel()
    orchestrator = MemoryOrchestrator(os.environ["TEST_DATABASE_URL"], vector_store, MagicMock())
    memory_id = orchestrator.create_candidate(
        identity(), "profile", f"prefers concise answers {uuid.uuid4()}", event_id
    )

    assert orchestrator.retrieve("answers", identity()) == []
    orchestrator.approve(memory_id, identity())
    assert [item["memory_id"] for item in orchestrator.retrieve("answers", identity())] == [
        memory_id
    ]
    assert orchestrator.retrieve("answers", identity("bob")) == []
    assert orchestrator.retrieve("answers", identity("alice", "other")) == []

    replacement_id = orchestrator.revise(
        memory_id, f"prefers detailed answers {uuid.uuid4()}", event_id, identity()
    )
    assert orchestrator.retrieve("answers", identity()) == []
    orchestrator.approve(replacement_id, identity())
    assert [item["memory_id"] for item in orchestrator.retrieve("answers", identity())] == [
        replacement_id
    ]

    orchestrator.delete("memory", replacement_id, identity())
    assert orchestrator.retrieve("answers", identity()) == []


def test_document_retrieval_is_tenant_scoped_and_deletion_removes_vector():
    store = VectorStore(database_url=os.environ["TEST_DATABASE_URL"])
    store.model = FakeEmbeddingModel()
    owner = identity("alice", f"document-acme-{uuid.uuid4()}")
    document_id = store.add_documents(
        [{"text": "tenant only document", "source": f"test://{uuid.uuid4()}"}], owner
    )[0]
    assert store.search_vector("document", owner, 1)[0]["text"] == "tenant only document"
    assert store.search_vector("document", {**owner, "tenant_id": "other"}, 1) == []

    orchestrator = MemoryOrchestrator(os.environ["TEST_DATABASE_URL"], store, MagicMock())
    orchestrator.delete("document", document_id, owner)
    assert store.search_vector("document", owner, 1) == []
