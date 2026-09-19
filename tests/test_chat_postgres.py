"""Real PostgreSQL/strict runtime/WS integration; object storage is an in-memory substitute."""

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import verifier_contracts
from core.agent_runtime import AgentRuntime
from core.evidence import EvidenceService, ObjectNotFound
from core.tool_contracts import ToolRegistry
from memory.context import ContextService, canonical_digest
from rag.answering import LOCAL_ABSTENTION, GroundedAnswering
from rag.runtime_tools import register_chat_tool
from webui import state
from webui.chat_requests import ChatRequests, RequestInProgress
from webui.routes import chat_tasks


class ObjectStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, body):
        self.objects[key] = body

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def copy(self, source, target):
        self.put(target, self.get(source))

    def delete(self, key):
        self.objects.pop(key, None)


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration required")
def test_websocket_and_http_persist_verified_chat_without_generation(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    identity = {"tenant_id": f"ec1-{uuid.uuid4()}", "username": "alice", "role": "user"}
    store = ObjectStore()
    tools = ToolRegistry()
    runtime = AgentRuntime(
        database_url, tools, evidence=EvidenceService(database_url, store, tools.sensitivity)
    )
    runtime.verifier_database_url = database_url
    context = ContextService(database_url)
    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = None
    monkeypatch.setattr(state, "agent_runtime", runtime)
    monkeypatch.setattr(state, "_context_service", lambda: context)
    monkeypatch.setattr(state, "_evidence_store", store)
    monkeypatch.setattr(
        verifier_contracts,
        "S3Utils",
        lambda *args, **kwargs: SimpleNamespace(get_object_body=lambda key: store.objects.get(key)),
    )
    monkeypatch.setattr(
        state,
        "_evidence_s3",
        SimpleNamespace(put_object=lambda key, body, _kind: store.put(key, body) or True),
    )
    register_chat_tool(
        tools,
        chat_adapter_runtime=object(),  # Any generation/status call fails this test.
        chat_answering=answering,
        chat_retriever=None,
        chat_context_loader=state._load_chat_context,
        chat_result_recorder=state._record_chat_result,
    )
    app = FastAPI()
    app.include_router(chat_tasks.router)
    app.dependency_overrides[chat_tasks.get_current_identity] = lambda: identity
    monkeypatch.setattr(chat_tasks, "decode_identity", lambda _token: identity)
    with TestClient(app) as client:
        request_id = str(uuid.uuid4())
        payload = {"query": "没有文档时能回答吗？", "request_id": request_id}
        with client.websocket_connect("/ws/chat?token=test") as socket:
            socket.send_json(payload)
            first = socket.receive_json()
        assert first.get("type") == "answer", first
        assert first["content"] == LOCAL_ABSTENTION
        assert first["model_execution"]["generation"] == "not_used"
        objects = dict(store.objects)
        replay = client.post("/api/chat", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json() == {
            key: value for key, value in first.items() if key not in {"type", "content"}
        }
        assert store.objects == objects
        assert len(context.events(first["session_id"], identity)) == 2
        status = client.get(f"/api/chat/requests/{request_id}")
        assert status.status_code == 200, status.text
        assert status.json()["task_id"] == first["task_id"]
        assert status.json()["state"] == "succeeded"
        conflict = client.post("/api/chat", json={**payload, "query": "different"})
        assert conflict.status_code == 409
        requests = ChatRequests(runtime.database)
        with requests.claim(request_id, None, payload["query"], identity):
            with pytest.raises(RequestInProgress):
                with requests.claim(request_id, None, payload["query"], identity):
                    pytest.fail("Concurrent request acquired the same lock")
        result = client.post(
            "/api/chat",
            json={
                "query": payload["query"],
                "session_id": first["session_id"],
                "request_id": str(uuid.uuid4()),
            },
        )
        assert result.status_code == 200, result.text
        second = result.json()
        assert first["run_id"] != second["run_id"]
        events = context.events(first["session_id"], identity)
        assert [event["event_type"] for event in events] == [
            "user_message",
            "assistant_message",
            "user_message",
            "assistant_message",
        ]
        for response in (first, second):
            task = runtime.get_task(response["task_id"], identity)
            assert task["state"] == "succeeded"
            assert task["run_id"] == response["run_id"]
            assert task["task_spec"]["execution_mode"] == "strict"
            checks = runtime.verifications(response["task_id"], identity)
            assert checks and all(check["status"] == "passed" for check in checks)
            feedback = json.loads(store.get(f"feedback/{response['feedback_id']}"))
            assert feedback["run_id"] == response["run_id"]
            assert runtime.evidence.manifest(response["run_id"], identity)

        # Fail after assistant persistence, then retry with a fresh service instance.
        save_feedback = chat_tasks.save_feedback

        def fail_feedback(*args, **kwargs):
            raise ConnectionError("simulated object store outage")

        monkeypatch.setattr(chat_tasks, "save_feedback", fail_feedback)
        interrupted = {"query": "recover", "request_id": str(uuid.uuid4())}
        assert client.post("/api/chat", json=interrupted).status_code == 500
        recovery_status = client.get(f"/api/chat/requests/{interrupted['request_id']}").json()
        assert recovery_status["state"] == "succeeded"
        monkeypatch.setattr(chat_tasks, "save_feedback", save_feedback)
        monkeypatch.setattr(state, "_context_service", lambda: ContextService(database_url))
        recovered = client.post("/api/chat", json=interrupted)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["task_id"] == recovery_status["task_id"]
        assert len(context.events(recovered.json()["session_id"], identity)) == 2

        for other in (
            {**identity, "username": "bob"},
            {**identity, "tenant_id": f"other-{uuid.uuid4()}"},
        ):
            app.dependency_overrides[chat_tasks.get_current_identity] = lambda other=other: other
            assert client.get(f"/api/chat/requests/{request_id}").status_code == 404
            result = client.post(
                "/api/chat", json={"query": "unauthorized", "session_id": first["session_id"]}
            )
            assert result.status_code == 404
        assert len(context.events(first["session_id"], identity)) == 4
        app.dependency_overrides[chat_tasks.get_current_identity] = lambda: identity
        invalid = client.post("/api/chat", json={**payload, "session_id": "not-a-uuid"})
        assert invalid.status_code == 422

        # A frozen context may outlive its permission. Reject BEFORE creating/running a task.
        doc_id = str(uuid.uuid4())
        with runtime.database.transaction(identity) as connection:
            connection.execute(
                "INSERT INTO documents (document_id, tenant_id, owner_id, source_uri, content_hash, status) "
                "VALUES (%s, %s, %s, %s, %s, 'ready')",
                (doc_id, identity["tenant_id"], identity["username"], "test://revoked", "a" * 64),
            )
        chunk_id = str(uuid.uuid4())
        text = "Orion timeout is 30 seconds."
        with runtime.database.transaction(identity) as connection:
            connection.execute(
                "INSERT INTO document_chunks (chunk_id, document_id, ordinal, text, lexemes, fts, embedding) "
                "VALUES (%s, %s, 0, %s, %s, to_tsvector('simple', %s), %s::vector)",
                (chunk_id, doc_id, text, text, text, json.dumps([0.0] * 512)),
            )
        retrieved = {
            "document_id": doc_id,
            "chunk_id": chunk_id,
            "text": text,
            "source": "test://revoked",
            "document_version": 1,
        }
        # Retrieval is substituted; the context, persisted text, verifier and ACL are real.
        grounded_context = ContextService(
            database_url, retriever=SimpleNamespace(retrieve=lambda *_args, **_kwargs: [retrieved])
        )
        monkeypatch.setattr(state, "_context_service", lambda: grounded_context)
        grounded_payload = {"query": "What is Orion's timeout?", "request_id": str(uuid.uuid4())}
        grounded = client.post("/api/chat", json=grounded_payload)
        assert grounded.status_code == 200, grounded.text
        assert grounded.json()["support_status"] == "extract_verified"
        assert grounded.json()["citations"][0]["quote"] == text
        source_feedback = json.loads(store.get(f"feedback/{grounded.json()['feedback_id']}"))
        assert source_feedback["answer_contract"]["answer_status"] == "answered"
        assert source_feedback["answer_policy_version"] == "rag-answer-v2"
        assert client.post("/api/chat", json=grounded_payload).json() == grounded.json()
        monkeypatch.setattr(state, "_context_service", lambda: context)
        revoked = {"query": "revoked", "request_id": str(uuid.uuid4())}
        with requests.claim(revoked["request_id"], None, revoked["query"], identity) as receipt:
            session = context.create_session(identity, session_id=receipt["session_id"])
            envelope = context.build_context(session["session_id"], revoked["query"], identity)
            envelope["retrieval_context"] = [
                {"document_id": doc_id, "text": "no longer authorized"}
            ]
            envelope["envelope_sha256"] = canonical_digest(
                {key: value for key, value in envelope.items() if key != "envelope_sha256"}
            )
            ref, digest = state._publish_chat_context(envelope, identity, receipt["run_id"])
            requests.save_context(receipt, ref, digest, identity)
        with runtime.database.transaction(identity) as connection:
            connection.execute(
                "UPDATE documents SET status = 'deleted' WHERE document_id = %s", (doc_id,)
            )
        rejected = client.post("/api/chat", json=revoked)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"] == "chat_context_no_longer_authorized"
        assert client.post("/api/chat", json=grounded_payload).status_code == 409
        with pytest.raises(PermissionError):
            runtime.get_task(receipt["task_id"], identity)

        with runtime.database.transaction(identity) as connection:
            connection.execute(
                "UPDATE conversation_sessions SET context_generation = context_generation + 1 WHERE session_id = %s",
                (first["session_id"],),
            )
        assert (
            client.post("/api/chat", json=payload).json()["detail"]
            == "chat_context_generation_changed"
        )
