import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.core.agent_runtime import AgentRuntime, ToolRegistry, ToolSpec
from src.memory.governance import MemoryGovernance
from src.memory.orchestrator import MemoryOrchestrator
from src.rag.vector_store import VectorStore

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


class Embeddings:
    def encode(self, values, convert_to_numpy=True):
        return np.array([[0.1] * 512 for _ in values])


def identity():
    return {"tenant_id": f"governance-{uuid.uuid4()}", "username": "alice", "role": "admin"}


def test_expiry_policy_is_recorded_and_replayable():
    owner = identity()
    runtime = AgentRuntime(os.environ["TEST_DATABASE_URL"], ToolRegistry())
    runtime.tools.register(ToolSpec(name="noop", handler=lambda _: {}))
    task = runtime.create_task(owner, "source", [{"tool": "noop"}])
    event_id = runtime.events(task["task_id"], owner)[0]["event_id"]
    store = VectorStore(database_url=os.environ["TEST_DATABASE_URL"])
    store.model = Embeddings()
    memories = MemoryOrchestrator(os.environ["TEST_DATABASE_URL"], store, MagicMock())
    memory_id = memories.create_candidate(
        owner, "profile", "expires", event_id, datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    memories.approve(memory_id, owner)
    governance = MemoryGovernance(os.environ["TEST_DATABASE_URL"])

    assert governance.expire_due(owner, "v1") == [memory_id]
    with governance.database.transaction(owner) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT policy_event_id FROM memory_policy_events WHERE memory_id = %s",
                (memory_id,),
            )
            policy_event_id = str(cursor.fetchone()["policy_event_id"])
    assert governance.replay(policy_event_id, owner)["after_json"] == {"status": "superseded"}
    governance.revert_expiry(policy_event_id, owner)
    assert memories.retrieve("expires", owner)[0]["memory_id"] == memory_id
