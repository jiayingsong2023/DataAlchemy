import os

import pytest

from src.connectors.git import GitConnector
from src.rag.vector_store import VectorStore
from src.storage.postgres import PostgresDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity(tenant_id="git-pilot", username="alice"):
    return {"tenant_id": tenant_id, "username": username, "role": "admin"}


def test_sync_advances_cursor_only_after_success(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    connector = GitConnector(database_url, "acme/platform")
    monkeypatch.setattr(
        connector,
        "_request",
        lambda _: [{"commit": {"author": {"date": "2026-07-30T00:00:00Z"}}}],
    )
    result = connector.sync(identity())
    assert result["commit_count"] == 1
    assert result["cursor"] == "2026-07-30T00:00:00Z"

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
    source = "github://acme/revoke/commit/deadbeef"
    store.add_documents([{"text": "private commit", "source": source}], identity())
    assert connector.revoke_source(source, identity()) == 1
    assert store.search_vector("private", identity()) == []
