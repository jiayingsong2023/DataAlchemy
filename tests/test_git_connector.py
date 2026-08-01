import json
import os
import uuid
from base64 import b64encode

import pytest

from src.connectors.git import GitConnector
from src.rag.chunkers.recursive import RecursiveChunker
from src.rag.vector_store import VectorStore
from src.storage.postgres import PostgresDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity(tenant_id="git-pilot", username="alice", role="admin"):
    return {"tenant_id": tenant_id, "username": username, "role": role}


class RawStore:
    def __init__(self):
        self.objects = {}

    def put_object(self, key, body, content_type="application/octet-stream"):
        self.objects[key] = (body, content_type)
        return True


def test_sync_advances_cursor_only_after_success(monkeypatch, tmp_path):
    database_url = os.environ["TEST_DATABASE_URL"]
    connector = GitConnector(database_url, "acme/platform")
    monkeypatch.setattr(
        connector,
        "_request",
        lambda _: [
            {
                "sha": "abc123",
                "commit": {
                    "author": {"date": "2026-07-30T00:00:00Z"},
                    "message": "pilot commit",
                },
            }
        ],
    )
    result = connector.sync(identity(), runs_dir=str(tmp_path))
    assert result["commit_count"] == 1
    assert result["cursor"] == "2026-07-30T00:00:00Z"
    assert (
        json.loads((tmp_path / "runs" / result["connector_run_id"] / "manifest.json").read_text())["state"]
        == "succeeded"
    )

    monkeypatch.setattr(connector, "_request", lambda _: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        connector.sync(identity())

    with PostgresDatabase(database_url).transaction(identity()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT cursor_value FROM connector_cursors WHERE connector_id = %s",
                (connector.connector_id,),
            )
            assert cursor.fetchone()["cursor_value"] == "2026-07-30T00:00:00Z"
            cursor.execute(
                "SELECT state FROM connector_runs WHERE connector_id = %s ORDER BY started_at DESC",
                (connector.connector_id,),
            )
            assert cursor.fetchone()["state"] == "failed"


def test_cursor_is_not_visible_across_tenants(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    connector = GitConnector(database_url, "acme/private")
    monkeypatch.setattr(connector, "_request", lambda _: [])
    connector.sync(identity("tenant-a"))
    with PostgresDatabase(database_url).transaction(identity("tenant-b")) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM connector_cursors WHERE connector_id = %s",
                (connector.connector_id,),
            )
            assert cursor.fetchone() is None


def test_source_revocation_removes_retrieval(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    connector = GitConnector(database_url, "acme/revoke")
    store = VectorStore(database_url=database_url)

    class Embeddings:
        def encode(self, values, convert_to_numpy=True):
            return [[0.1] * 512 for _ in values]

    store.model = Embeddings()
    source = f"github://acme/revoke/commit/{uuid.uuid4()}"
    marker = str(uuid.uuid4())
    store.add_documents([{"text": marker, "source": source}], identity())
    assert connector.revoke_source(source, identity()) == 1
    assert store.search_text(marker, identity()) == []


def test_sync_stores_file_content_and_retires_prior_revision(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    raw_store = RawStore()
    connector = GitConnector(database_url, f"acme/files-{uuid.uuid4()}", raw_store=raw_store)
    store = VectorStore(database_url=database_url)

    class Embeddings:
        def encode(self, values, convert_to_numpy=True):
            return [[0.1] * 512 for _ in values]

    store.model = Embeddings()
    marker = str(uuid.uuid4())

    def request(path):
        if path.startswith("commits?"):
            return [
                {
                    "sha": "first",
                    "commit": {"author": {"date": "2026-07-30T00:00:00Z"}},
                }
            ]
        if path == "commits/first":
            return {"files": [{"filename": "docs/runbook.md", "status": "added"}]}
        return {"encoding": "base64", "content": b64encode(f"{marker} v1".encode()).decode()}

    monkeypatch.setattr(connector, "_request", request)
    first = connector.sync(identity(), store, [("user", "reader")])
    assert first["document_count"] == 1
    assert raw_store.objects
    with PostgresDatabase(database_url).transaction(identity()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, raw_object_key FROM connector_ingest_items "
                "WHERE connector_id = %s",
                (connector.connector_id,),
            )
            item = cursor.fetchone()
    assert item["state"] == "indexed"
    assert item["raw_object_key"] in raw_store.objects
    reader = identity("git-pilot", "reader", "user")
    assert store.search_text(marker, reader)

    def updated(path):
        if path.startswith("commits?"):
            return [
                {
                    "sha": "second",
                    "commit": {"author": {"date": "2026-07-30T01:00:00Z"}},
                }
            ]
        if path == "commits/second":
            return {"files": [{"filename": "docs/runbook.md", "status": "modified"}]}
        return {"encoding": "base64", "content": b64encode(f"{marker} v2".encode()).decode()}

    monkeypatch.setattr(connector, "_request", updated)
    connector.sync(identity(), store, [("user", "reader")])
    results = store.search_text(marker, reader)
    assert len(results) == 1
    assert results[0]["text"] == f"{marker} v2"

    connector.sync(identity(), store, [])
    assert store.search_text(marker, reader) == []
    assert store.search_text(marker, identity())


def test_sync_lands_but_never_indexes_a_secret(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    raw_store = RawStore()
    connector = GitConnector(database_url, f"acme/secrets-{uuid.uuid4()}", raw_store=raw_store)
    store = VectorStore(database_url=database_url)

    class Embeddings:
        def encode(self, values, **_):
            return [[0.1] * 512 for _ in values]

    store.model = Embeddings()

    def request(path):
        if path.startswith("commits?"):
            return [{"sha": "secret", "commit": {"author": {"date": "2026-07-30T02:00:00Z"}}}]
        if path == "commits/secret":
            return {"files": [{"filename": "config.py", "status": "added"}]}
        return {
            "encoding": "base64",
            "content": b64encode(b"API_KEY = 'very-secret-value'").decode(),
        }

    monkeypatch.setattr(connector, "_request", request)
    result = connector.sync(identity(), store)
    assert result["document_count"] == 0
    assert result["rejected_count"] == 1
    assert raw_store.objects
    with PostgresDatabase(database_url).transaction(identity()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, rejection_reason FROM connector_ingest_items "
                "WHERE connector_id = %s",
                (connector.connector_id,),
            )
            assert cursor.fetchone() == {"state": "rejected", "rejection_reason": "secret_detected"}


def test_vector_store_publishes_one_document_with_multiple_chunks():
    database_url = os.environ["TEST_DATABASE_URL"]
    store = VectorStore(database_url=database_url)

    class Embeddings:
        def encode(self, values, **_):
            return [[0.1] * 512 for _ in values]

    store.model = Embeddings()
    document_id = store.add_documents(
        [{"text": "alpha beta gamma delta", "source": f"test://chunked-{uuid.uuid4()}"}],
        identity(),
        RecursiveChunker(chunk_size=8, chunk_overlap=0),
    )[0]
    with PostgresDatabase(database_url).transaction(identity()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS count FROM document_chunks WHERE document_id = %s",
                (document_id,),
            )
            assert cursor.fetchone()["count"] > 1
