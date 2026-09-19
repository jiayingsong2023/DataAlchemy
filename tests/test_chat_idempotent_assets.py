"""Replay assets without duplicating durable conversation or feedback records."""

import os
import uuid

import pytest

from core.evidence import ObjectNotFound
from feedback import save_feedback
from memory.context import ContextService


def test_feedback_replay_is_immutable_and_storage_errors_propagate():
    class Store:
        def __init__(self):
            self.objects = {}
            self.writes = 0
            self.error = None

        def get(self, key):
            if self.error:
                raise self.error
            if key not in self.objects:
                raise ObjectNotFound(key)
            return self.objects[key]

        def put(self, key, body):
            self.writes += 1
            self.objects[key] = body

    store = Store()
    options = {
        "feedback_id": f"feedback_{uuid.uuid4()}.json",
        "timestamp": "2026-09-18T12:00:00+00:00",
        "evidence_store": store,
    }
    assert save_feedback(None, "question", "answer", **options) == options["feedback_id"]
    assert save_feedback(None, "question", "answer", **options) == options["feedback_id"]
    assert store.writes == 1
    with pytest.raises(ValueError, match="feedback_source_conflict"):
        save_feedback(None, "question", "changed", **options)
    store.error = OSError("storage unavailable")
    with pytest.raises(OSError, match="storage unavailable"):
        save_feedback(None, "question", "answer", **options)
    assert store.writes == 1
    for unsafe in ("../other.json", "feedback_../other.json", "feedback_invalid.json"):
        with pytest.raises(ValueError, match="feedback_id_invalid"):
            save_feedback(None, "q", "a", **{**options, "feedback_id": unsafe})


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration required")
def test_session_and_event_replay_preserve_versions_and_enforce_identity():
    service = ContextService(os.environ["TEST_DATABASE_URL"])
    identity = {"tenant_id": f"replay-{uuid.uuid4()}", "username": "alice", "role": "user"}
    session_id, event_id = str(uuid.uuid4()), str(uuid.uuid4())
    first = service.create_session(identity, session_id=session_id)
    assert service.create_session(identity, session_id=session_id) == first
    event = service.append_event(
        session_id, "user_message", {"content": "question"}, identity, event_id=event_id
    )
    version = service.get_session(session_id, identity)["version"]
    assert (
        service.append_event(
            session_id,
            "user_message",
            {"content": "question"},
            identity,
            event_id=event_id,
            expected_version=first["version"],
        )
        == event
    )
    assert service.get_session(session_id, identity)["version"] == version
    for overrides in (
        {"content": {"content": "different"}},
        {"event_type": "assistant_message"},
        {"trust_label": "trusted_system"},
        {"task_id": str(uuid.uuid4())},
        {"run_id": str(uuid.uuid4())},
        {"agent_event_id": str(uuid.uuid4())},
    ):
        arguments = {
            "session_id": session_id,
            "event_type": "user_message",
            "content": {"content": "question"},
            "identity": identity,
            "event_id": event_id,
        }
        with pytest.raises(ValueError, match="conversation_event_id_conflict"):
            service.append_event(**{**arguments, **overrides})
    other_session = service.create_session(identity)["session_id"]
    with pytest.raises(ValueError, match="conversation_event_id_conflict"):
        service.append_event(
            other_session, "user_message", {"content": "question"}, identity, event_id=event_id
        )
    for other in (
        {**identity, "username": "bob"},
        {**identity, "tenant_id": f"other-{uuid.uuid4()}"},
    ):
        with pytest.raises(PermissionError):
            service.create_session(other, session_id=session_id)
        with pytest.raises(PermissionError):
            service.append_event(
                session_id, "user_message", {"content": "question"}, other, event_id=event_id
            )
    with service.database.transaction(identity) as connection:
        connection.execute(
            "UPDATE conversation_sessions SET state = 'closed' WHERE session_id = %s",
            (session_id,),
        )
    assert (
        service.append_event(
            session_id, "user_message", {"content": "question"}, identity, event_id=event_id
        )
        == event
    )
    with pytest.raises(ValueError, match="Session is not active"):
        service.append_event(session_id, "user_message", {"content": "new"}, identity)
    assert len(service.events(session_id, identity)) == 1


def test_invalid_session_and_event_ids_fail_before_database_access():
    service = ContextService.__new__(ContextService)
    with pytest.raises(ValueError):
        service.create_session({}, session_id="not-a-uuid")
    with pytest.raises(ValueError):
        service.append_event("unused", "user_message", {}, {}, event_id="not-a-uuid")
