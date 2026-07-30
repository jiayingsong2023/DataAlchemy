import pytest

from src.core import runtime_tools
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
    assert registry.get("sync_git").requires_approval
    assert registry.get("sync_git").uses_identity
    assert registry.get("ingest_document").requires_approval
    assert registry.get("ingest_document").uses_identity


def test_ingest_document_reads_only_the_raw_documents_prefix(monkeypatch):
    stored = []

    class ObjectStore:
        def get_object_body(self, key):
            assert key == "raw/documents/pilot.md"
            return b"# Pilot\n\nThe support window is Tuesday."

    class VectorStore:
        def add_documents(self, documents, identity, chunker):
            stored.extend(documents)
            assert identity["tenant_id"] == "acme"
            assert chunker is not None
            return ["document-1"]

    class AgentManager:
        agent_c = type("AgentC", (), {"vs": VectorStore()})()

        def lazy_load_agents(self, **_):
            return None

    class Audit:
        def record(self, *_, **kwargs):
            assert kwargs["metadata"] == {"object_key": "raw/documents/pilot.md"}

    coordinator = type("Coordinator", (), {"agent_manager": AgentManager()})()
    monkeypatch.setattr(runtime_tools, "S3Utils", lambda: ObjectStore())
    monkeypatch.setattr(runtime_tools, "AuditLog", lambda _: Audit())

    result = runtime_tools._ingest_document(
        coordinator,
        {
            "object_key": "raw/documents/pilot.md",
            "_identity": {"tenant_id": "acme", "username": "alice", "role": "admin"},
        },
    )
    assert result["document_id"] == "document-1"
    assert stored[0]["metadata"]["raw_object_key"] == "raw/documents/pilot.md"
