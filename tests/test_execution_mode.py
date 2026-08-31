import pytest

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
