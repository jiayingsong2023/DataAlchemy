from contextlib import contextmanager

import webui.app as app


def test_run_feedback_is_indexed_once(monkeypatch):  # noqa: C901 - local test doubles
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return None

        def fetchone(self):
            return None

    class Database:
        def __init__(self, _url):
            pass

        @contextmanager
        def transaction(self, _identity):
            yield type("Connection", (), {"cursor": lambda _self: Cursor()})()

    class S3:
        def __init__(self):
            self.objects = []

        def put_object(self, **kwargs):
            self.objects.append(kwargs)

    class Evaluation:
        def __init__(self, _url):
            pass

        def create_annotation(self, identity, **kwargs):
            assert identity["tenant_id"] == "tenant"
            assert kwargs["content_key"].endswith(".source")
            return "annotation-1"

    s3 = S3()
    monkeypatch.setattr(app, "PostgresDatabase", Database)
    monkeypatch.setattr(app, "EvaluationService", Evaluation)
    monkeypatch.setattr(app, "get_s3_client", lambda: s3)
    annotation = app._index_feedback_annotation(
        {"tenant_id": "tenant", "username": "owner", "role": "user"},
        {"run_id": "run-1", "feedback": "good", "query": "q", "answer": "a"},
        "feedback/id.json",
        b"{}",
    )
    assert annotation == "annotation-1"
    assert s3.objects[0]["Key"] == "feedback/id.json.source"
