import json

import pytest

from src.feedback import save_feedback


def test_feedback_source_is_immutable_and_write_failure_is_fatal():
    class Store:
        def __init__(self):
            self.objects = []

        def put_object(self, key, body, content_type):
            self.objects.append((key, json.loads(body), content_type))
            return len(self.objects) == 1

    store = Store()
    feedback_id = save_feedback(
        store,
        "question",
        "answer",
        owner="alice",
        tenant_id="acme",
        run_id="run-1",
        citations=[
            {
                "source_span_ids": ["span-1"],
                "source_content_sha256": "a" * 64,
                "acl_digest": "acl-1",
            }
        ],
        retrieval_report={"context_snapshot_id": "snapshot-1"},
        model_execution={"model_id": "model-a"},
    )
    assert store.objects[0][0] == f"feedback/{feedback_id}"
    assert store.objects[0][1]["run_id"] == "run-1"
    assert store.objects[0][1]["citations"][0]["source_span_ids"] == ["span-1"]
    assert store.objects[0][1]["retrieval_report"]["context_snapshot_id"] == "snapshot-1"
    with pytest.raises(RuntimeError, match="feedback_source_write_failed"):
        save_feedback(store, "question", "answer")
