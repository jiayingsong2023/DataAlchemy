"""WebSocket transport checks with a stubbed business service, not end-to-end tests."""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from webui.routes import chat_tasks
from webui.schemas import ChatResponse


@pytest.fixture
def chat_client(monkeypatch):
    identity = {"tenant_id": "acme", "username": "alice", "role": "user"}
    calls = []

    async def execute(request, actual_identity):
        assert actual_identity == identity
        calls.append(request)
        if request.session_id == "missing":
            raise HTTPException(404, "Session not found")
        if request.session_id == "closed":
            raise HTTPException(409, "Session is not active")
        return ChatResponse(
            answer="answer",
            feedback_id="feedback-1",
            session_id="session-1",
            run_id="server-run",
            task_id="server-task",
        )

    monkeypatch.setattr(
        chat_tasks, "decode_identity", lambda token: identity if token == "valid" else None
    )
    monkeypatch.setattr(chat_tasks, "_execute_chat", execute)
    app = FastAPI()
    app.include_router(chat_tasks.router)
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize("path", ["/ws/chat", "/ws/chat?token=invalid"])
def test_websocket_rejects_missing_or_invalid_identity(chat_client, path):
    client, calls = chat_client
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(path):
            pytest.fail("unauthenticated socket accepted")
    assert caught.value.code == 1008
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    ["{", "[]", "null", json.dumps({"query": " \t\n"}), json.dumps({"query": "x" * 10001})],
    ids=["invalid-json", "array", "null", "blank", "over-limit"],
)
def test_websocket_invalid_message_does_not_close_connection(chat_client, payload):
    client, calls = chat_client
    with client.websocket_connect("/ws/chat?token=valid") as websocket:
        websocket.send_text(payload)
        assert websocket.receive_json() == {"error": "Invalid chat request", "status_code": 422}
        assert calls == []
        websocket.send_json({"query": "valid question", "run_id": "caller-run"})
        answer = websocket.receive_json()
        assert answer["type"] == "answer"
        assert answer["content"] == answer["answer"] == "answer"
        assert answer["run_id"] == "server-run"
        assert answer["task_id"] == "server-task"
        assert answer["feedback_id"] == "feedback-1"
        assert answer["session_id"] == "session-1"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("session_id", "status_code", "message"),
    [("missing", 404, "Session not found"), ("closed", 409, "Session is not active")],
)
def test_websocket_session_error_does_not_close_connection(
    chat_client, session_id, status_code, message
):
    client, calls = chat_client
    with client.websocket_connect("/ws/chat?token=valid") as websocket:
        websocket.send_json({"query": "question", "session_id": session_id})
        assert websocket.receive_json() == {"error": message, "status_code": status_code}
        websocket.send_json({"query": "valid question"})
        assert websocket.receive_json()["type"] == "answer"
    assert len(calls) == 2
