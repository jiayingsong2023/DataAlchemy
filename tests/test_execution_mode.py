import pytest

from src.agents import agent_d
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
