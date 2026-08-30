import pytest

from src.agents import agent_d
from src.agents import coordinator as coordinator_module
from src.core import agent_manager as agent_manager_module
from src.etl import sanitizers


def test_local_mode_never_creates_a_cloud_client(monkeypatch):
    monkeypatch.setattr(agent_d, "EXECUTION_MODE", "local")
    agent = agent_d.AgentD()

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

    assert await coordinator.chat_async("question", {"tenant_id": "acme"}, context=[]) == "ok"
    assert labels == [{"entrypoint": "chat_async", "route": "direct"}]
