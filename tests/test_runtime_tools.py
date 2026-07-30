import pytest

from src.core.agent_runtime import ToolRegistry
from src.core.runtime_tools import register_coordinator_tools


class Coordinator:
    def __init__(self):
        self.calls = []

    async def chat_async(self, query, identity):
        self.calls.append(("chat", query, identity))
        return "answer"

    def run_ingestion_pipeline(self, **kwargs):
        self.calls.append(("ingest", kwargs))

    def run_training_pipeline(self):
        self.calls.append(("train",))

    def reload_model(self):
        self.calls.append(("release",))
        return True


@pytest.mark.asyncio
async def test_existing_coordinator_capabilities_are_registered_as_tools():
    coordinator = Coordinator()
    registry = ToolRegistry()
    register_coordinator_tools(registry, coordinator)

    assert await registry.get("rag_chat").handler(
        {"query": "hello", "_identity": {"tenant_id": "acme", "username": "alice", "role": "user"}}
    ) == {"answer": "answer"}
    assert registry.get("ingest").requires_approval
    assert registry.get("train").idempotent
    assert registry.get("release").roles == frozenset({"admin"})
