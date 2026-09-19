import json

import pytest

from src.etl import sanitizers
from src.rag import answering


def test_local_mode_never_creates_a_cloud_client(monkeypatch):
    monkeypatch.setattr(answering, "EXECUTION_MODE", "local")
    agent = answering.GroundedAnswering()

    assert agent.client is None
    assert agent.fuse_and_respond("question", [], "local answer") == "现有文档没有说明这个问题。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        [],
        [
            {
                "text": "令狐冲转生后变成了一只史莱姆。",
                "context_type": "document",
                "document_id": "doc",
                "chunk_id": "chunk",
            }
        ],
    ],
)
async def test_local_answer_does_not_depend_on_generation(monkeypatch, context):
    monkeypatch.setattr(answering, "EXECUTION_MODE", "local")
    agent = answering.GroundedAnswering()

    class UnavailableAdapter:
        async def predict_async(self, *_args, **_kwargs):
            pytest.fail("local answering must not call generation")

        def model_status(self, *_args):
            pytest.fail("local answering must not query generation model status")

    traces = []
    query = "令狐冲转生后变成了什么？"
    answer, citations, execution = await answering.answer_with_citations(
        query,
        {"tenant_id": "acme", "username": "alice", "role": "user"},
        context,
        UnavailableAdapter(),
        agent,
        trace_recorder=traces.append,
    )
    assert answer == answering.local_evidence_answer(query, context)
    assert len(citations) == len(context)
    if citations:
        assert citations[0]["quote"] == context[0]["text"]
    assert execution == {"tenant_id": "acme", "generation": "not_used"}
    assert traces == []


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
            message = type(
                "Message",
                (),
                {
                    "content": json.dumps(
                        {
                            "answer": "grounded answer",
                            "answer_status": "answered",
                            "citations": [{"chunk_id": "chunk", "quote": "private"}],
                        }
                    )
                },
            )()
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
        [
            {
                "text": "private",
                "metadata": {"source": "guide"},
                "context_type": "document",
                "document_id": "doc",
                "chunk_id": "chunk",
            }
        ],
        "intuition",
        trace_recorder=traces.append,
    )

    assert answer == "grounded answer"
    assert calls[0]["messages"][1]["content"] == "[REDACTED]"
    assert traces[0]["component"] == "agent_d.fusion"
    assert traces[0]["status"] == "succeeded"
