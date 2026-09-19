import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui import state
from webui.routes import chat_tasks


def _send_chat(client, transport, payload):
    if transport == "http":
        response = client.post("/api/chat", json=payload)
        return response.status_code, response.json()
    with client.websocket_connect("/ws/chat?token=test") as socket:
        socket.send_json(payload)
        body = socket.receive_json()
        status_code = body.get("status_code", 200)
        assert body.get("type") == ("answer" if status_code == 200 else None)
        return status_code, body


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize(
    "task_state", ["succeeded", "verification_failed", "evidence_pending", "bad_hash"]
)
async def test_chat_returns_server_run_and_binds_session_and_feedback(
    monkeypatch, transport, task_state
):
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
            calls["context_thread"] = threading.get_ident()
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
            calls["runtime_thread"] = threading.get_ident()
            return {
                "task_id": task_id,
                "state": "succeeded" if task_state == "bad_hash" else task_state,
                "finish_reason": None,
            }

        def tool_runs(self, _task_id, _identity):
            return [
                {
                    "result": {
                        "output": {
                            "response_ref": "response.json",
                            "response_sha256": (
                                "0" * 64
                                if task_state == "bad_hash"
                                else chat_tasks.sha256(response_body)
                            ),
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

    identity = {"tenant_id": "acme", "username": "alice", "role": "user"}
    app = FastAPI()
    app.include_router(chat_tasks.router)
    app.dependency_overrides[chat_tasks.get_current_identity] = lambda: identity
    monkeypatch.setattr(chat_tasks, "decode_identity", lambda _: identity)
    with TestClient(app) as client:
        payload = {"query": "question", "run_id": "caller-controlled"}
        status_code, body = _send_chat(client, transport, payload)

    if task_state != "succeeded":
        assert status_code == (409 if task_state == "verification_failed" else 500)
        assert "feedback_run_id" not in calls
        assert [event[0] for event in calls["events"]] == ["user_message"]
        return
    assert status_code == 200
    response = chat_tasks.ChatResponse(**body)

    assert response.run_id != "caller-controlled"
    assert calls["task"]["run_id"] == response.run_id
    assert calls["task"]["task_id"] == response.task_id
    assert calls["task"]["execution_mode"] == "strict"
    assert calls["task"]["task_spec"]["success_criteria"][0]["verifier"] == ("verify_chat_capture")
    assert (
        calls["task"]["task_spec"]["success_criteria"][0]["parameters"]["context_sha256"]
        == "a" * 64
    )
    assert calls["context_task"]["run_id"] == response.run_id
    assert calls["context_thread"] != calls["runtime_thread"]
    assert calls["feedback_run_id"] == response.run_id
    assert calls["feedback_citations"] == []
    assert calls["feedback_context_sha256"] == "a" * 64
    assert all(lineage["run_id"] == response.run_id for _, lineage in calls["events"])
