"""Read-only GitHub repository connector for the Phase 3 pilot."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid
from typing import Any

from rag.vector_store import VectorStore
from storage.postgres import PostgresDatabase


class GitConnector:
    """Incrementally list repository commits without retaining credentials."""

    def __init__(self, database_url: str, repository: str, token: str = ""):
        self.database = PostgresDatabase(database_url)
        self.repository = repository.strip("/")
        self.token = token
        self.connector_id = f"github:{self.repository}"

    def _request(self, path: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def sync(
        self,
        identity: dict[str, str],
        vector_store: VectorStore | None = None,
        acl: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Return commits newer than the saved cursor; only advance on success."""
        run_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT cursor_value FROM connector_cursors WHERE connector_id = %s",
                    (self.connector_id,),
                )
                row = cursor.fetchone()
                before = row["cursor_value"] if row else None
                cursor.execute(
                    "INSERT INTO connector_runs "
                    "(run_id, connector_id, tenant_id, state, cursor_before) "
                    "VALUES (%s, %s, %s, 'running', %s)",
                    (run_id, self.connector_id, identity["tenant_id"], before),
                )
        try:
            query = "commits?per_page=100"
            if before:
                query += "&since=" + urllib.parse.quote(before)
            commits = self._request(query)
            after = max((item["commit"]["author"]["date"] for item in commits), default=before)
            documents = [
                {
                    "text": item["commit"]["message"],
                    "source": f"github://{self.repository}/commit/{item['sha']}",
                    "metadata": {"source_version": item["sha"], "acl": acl or []},
                }
                for item in commits
            ]
            document_ids = vector_store.add_documents(documents, identity) if vector_store else []
        except Exception as error:
            with self.database.transaction(identity) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE connector_runs SET state = 'failed', error_summary = %s, "
                        "completed_at = now() WHERE run_id = %s",
                        (str(error)[:500], run_id),
                    )
            raise
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO connector_cursors (connector_id, tenant_id, cursor_value) "
                    "VALUES (%s, %s, %s) ON CONFLICT (connector_id) DO UPDATE "
                    "SET cursor_value = EXCLUDED.cursor_value, updated_at = now()",
                    (self.connector_id, identity["tenant_id"], after),
                )
                cursor.execute(
                    "UPDATE connector_runs SET state = 'succeeded', cursor_after = %s, "
                    "completed_at = now() WHERE run_id = %s",
                    (after, run_id),
                )
        return {
            "run_id": run_id,
            "commit_count": len(commits),
            "document_count": len(document_ids),
            "cursor": after,
        }

    def revoke_source(self, source_uri: str, identity: dict[str, str]) -> int:
        """Apply a source deletion before the next retrieval can observe it."""
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document_id FROM documents WHERE source_uri = %s AND status = 'ready'",
                    (source_uri,),
                )
                document_ids = [row["document_id"] for row in cursor.fetchall()]
                cursor.execute(
                    "UPDATE documents SET status = 'deleted', deleted_at = now() "
                    "WHERE source_uri = %s AND status = 'ready'",
                    (source_uri,),
                )
                if document_ids:
                    cursor.execute(
                        "DELETE FROM document_chunks WHERE document_id = ANY(%s)",
                        (document_ids,),
                    )
        return len(document_ids)
