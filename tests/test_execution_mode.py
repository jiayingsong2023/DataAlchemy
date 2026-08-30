import pytest

from rag.answering import GroundedAnswering as RuntimeGroundedAnswering
from src.agents import coordinator as coordinator_module
from src.agents.agent_d import AgentD
from src.core import agent_manager as agent_manager_module
from src.etl import sanitizers
from src.rag import answering


def test_local_mode_never_creates_a_cloud_client(monkeypatch):
    monkeypatch.setattr(answering, "EXECUTION_MODE", "local")
    agent = answering.GroundedAnswering()

    assert agent.client is None
    assert agent.fuse_and_respond("question", [], "local answer") == "现有文档没有说明这个问题。"


def test_cloud_mode_fails_closed_without_presidio(monkeypatch):
    monkeypatch.setattr(sanitizers, "presidio_engine", None)

    with pytest.raises(RuntimeError, match="Presidio"):
        sanitizers.sanitize_for_cloud("email@example.com")


def test_kubernetes_agent_loads_only_when_requested(monkeypatch):
    created = []

    class AgentA:
        def __init__(self, mode):
            created.append(mode)

    monkeypatch.setattr(agent_manager_module, "AgentA", AgentA)
    manager = agent_manager_module.AgentManager(mode="python")

    assert manager.agent_a is None
    assert created == []

    manager.lazy_load_agents(need_a=True)

    assert isinstance(manager.agent_a, AgentA)
    assert created == ["python"]


@pytest.mark.asyncio
async def test_legacy_coordinator_records_direct_calls(monkeypatch):
    labels = []

    class Counter:
        def labels(self, **values):
            labels.append(values)
            return self

        def inc(self):
            pass

    class AgentB:
        async def predict_async(self, *_args, **_kwargs):
            return "intuition"

        def model_status(self, identity):
            return {"tenant_id": identity["tenant_id"], "model_id": "model-a"}

    class AgentManager:
        agent_b = AgentB()
        agent_d = type(
            "AgentD", (), {"fuse_and_respond": staticmethod(lambda *_args, **_kwargs: "ok")}
        )()

        def lazy_load_agents(self, **_kwargs):
            pass

    monkeypatch.setattr(coordinator_module, "LEGACY_AGENT_CALLS", Counter())
    coordinator = coordinator_module.Coordinator.__new__(coordinator_module.Coordinator)
    coordinator.agent_manager = AgentManager()

    identity = {"tenant_id": "acme"}
    assert await coordinator.chat_async("question", identity, context=[]) == "ok"
    answer, citations, model_execution = await coordinator.chat_with_citations_async(
        "question",
        identity,
        context=[
            {
                "context_type": "document",
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "source": "minio://documents/guide.pdf",
                "metadata": {
                    "source_version": "sha256:" + "a" * 64,
                    "locator": {"page": 3},
                },
            }
        ],
        route="runtime_adapter",
    )

    assert answer == "ok"
    assert citations == [
        {
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "source_uri": "minio://documents/guide.pdf",
            "source_version": "sha256:" + "a" * 64,
            "source_sha256": "a" * 64,
            "locator": {"page": 3},
        }
    ]
    assert model_execution == {"tenant_id": "acme", "model_id": "model-a"}
    assert labels == [
        {"entrypoint": "chat_async", "route": "direct"},
        {"entrypoint": "chat_with_citations_async", "route": "runtime_adapter"},
    ]


def test_cloud_fusion_sanitizes_before_call_and_records_trace(monkeypatch):
    calls = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            message = type("Message", (), {"content": "grounded answer"})()
            choice = type("Choice", (), {"message": message})()
            return type(
                "Response",
                (),
                {"choices": [choice], "usage": None, "model": "model-a", "id": "call-1"},
            )()

    agent = answering.GroundedAnswering.__new__(answering.GroundedAnswering)
    agent.client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": Completions()})()}
    )()
    agent.model = "model-a"
    agent.temperature = 0.0
    agent.max_tokens = 64
    traces = []
    monkeypatch.setattr(answering, "sanitize_for_cloud", lambda _text: "[REDACTED]")
    monkeypatch.setattr(answering, "record_cloud_call", lambda *_args, **_kwargs: "audit-1")

    answer = agent.fuse_and_respond(
        "email alice@example.com",
        [{"text": "private", "metadata": {"source": "guide"}}],
        "intuition",
        trace_recorder=traces.append,
    )

    assert answer == "grounded answer"
    assert calls[0]["messages"][1]["content"] == "[REDACTED]"
    assert traces[0]["component"] == "agent_d.fusion"
    assert traces[0]["status"] == "succeeded"


def test_agent_d_is_a_thin_compatibility_name():
    assert issubclass(AgentD, RuntimeGroundedAnswering)
