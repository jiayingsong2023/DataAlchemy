import json

import pytest

from webui import state
from webui.routes import chat_tasks


@pytest.mark.asyncio
async def test_chat_returns_server_run_and_binds_session_and_feedback(monkeypatch):
    response_body = json.dumps(
        {
            "answer": "answer",
            "citations": [],
            "model_execution": {"model_id": "model-a"},
        }
    ).encode()
    calls = {"events": []}

    class Context:
        def create_session(self, _identity):
            return {"session_id": "session-1"}

        def append_event(self, _session_id, event_type, _content, _identity, **lineage):
            calls["events"].append((event_type, lineage))
            return {"event_id": "user-event"}

        def build_context(self, _session_id, _query, _identity, **kwargs):
            calls["context_task"] = kwargs["task"]
            return {
                "snapshot_id": "snapshot-1",
                "envelope_sha256": "a" * 64,
                "retrieval_context": [],
            }

    class Runtime:
        def create_task(self, _identity, _goal, _plan, **kwargs):
            calls["task"] = kwargs
            return {"task_id": kwargs["task_id"], "run_id": kwargs["run_id"]}

        async def run(self, task_id, _identity):
            return {"task_id": task_id, "state": "succeeded", "finish_reason": None}

        def tool_runs(self, _task_id, _identity):
            return [
                {
                    "result": {
                        "output": {
                            "response_ref": "response.json",
                            "response_sha256": chat_tasks.sha256(response_body),
                        }
                    }
                }
            ]

    class Store:
        def get(self, key):
            assert key == "response.json"
            return response_body

    def save_feedback(_store, _query, _answer, **kwargs):
        calls["feedback_run_id"] = kwargs["run_id"]
        calls["feedback_citations"] = kwargs["citations"]
        calls["feedback_context_sha256"] = kwargs["retrieval_report"]["context_sha256"]
        return "feedback-1"

    monkeypatch.setattr(state, "_context_service", lambda: Context())
    monkeypatch.setattr(state, "_publish_chat_context", lambda *_: ("context.json", "b" * 64))
    monkeypatch.setattr(state, "agent_runtime", Runtime())
    monkeypatch.setattr(state, "_evidence_store", Store())
    monkeypatch.setattr(chat_tasks, "save_feedback", save_feedback)

    response = await chat_tasks.chat(
        chat_tasks.ChatRequest(query="question", run_id="caller-controlled"),
        {"tenant_id": "acme", "username": "alice", "role": "user"},
    )

    assert response.run_id != "caller-controlled"
    assert calls["task"]["run_id"] == response.run_id
    assert calls["task"]["execution_mode"] == "strict"
    assert calls["task"]["task_spec"]["success_criteria"][0]["verifier"] == ("verify_chat_capture")
    assert (
        calls["task"]["task_spec"]["success_criteria"][0]["parameters"]["context_sha256"]
        == "a" * 64
    )
    assert calls["context_task"]["run_id"] == response.run_id
    assert calls["feedback_run_id"] == response.run_id
    assert calls["feedback_citations"] == []
    assert calls["feedback_context_sha256"] == "a" * 64
    assert all(lineage["run_id"] == response.run_id for _, lineage in calls["events"])
