import json

from src.feedback import rate_feedback


def test_feedback_rating_writes_immutable_source_and_annotation():
    source = {
        "query": "q",
        "answer": "a",
        "owner": "owner",
        "tenant_id": "tenant",
        "run_id": "run-1",
    }

    class Store:
        def __init__(self):
            self.writes = []

        def get_object_body(self, key):
            assert key == "feedback/id.json"
            return json.dumps(source).encode()

        def put_object(self, key, body, content_type):
            self.writes.append((key, body, content_type))
            return True

    class Evaluation:
        def create_annotation(self, identity, **kwargs):
            assert identity["tenant_id"] == "tenant"
            assert kwargs["content_key"].startswith("feedback/ratings/id.json/")
            assert kwargs["label"]["feedback"] == "good"
            return "annotation-1"

    store = Store()
    annotation = rate_feedback(
        store,
        Evaluation(),
        {"tenant_id": "tenant", "username": "owner", "role": "user"},
        "id.json",
        "good",
    )
    assert annotation == "annotation-1"
    assert store.writes[0][0].startswith("feedback/ratings/id.json/")
