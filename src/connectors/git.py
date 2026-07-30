"""Read-only GitHub repository connector for the Phase 3 pilot."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request
import uuid
from typing import Any

from connectors.git_ingestion import prepare_git_document
from rag.vector_store import VectorStore
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase
from storage.run_assets import publish_run
from utils.s3_utils import S3Utils


class GitConnector:
    """Incrementally list repository commits without retaining credentials."""

    def __init__(
        self, database_url: str, repository: str, token: str = "", raw_store: Any | None = None
    ):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)
        self.repository = repository.strip("/")
        self.token = token
        self.connector_id = f"github:{self.repository}"
        self.raw_store = raw_store or S3Utils()

    def _request(self, path: str) -> Any:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def _documents(
        self,
        commits: list[dict[str, Any]],
        acl: list[tuple[str, str]],
        identity: dict[str, str],
        run_id: str,
    ) -> tuple[list[tuple[dict[str, Any], object, str]], int]:
        documents: list[tuple[dict[str, Any], object, str]] = []
        rejected = 0
        for commit in commits:
            revision = commit["sha"]
            detail = self._request(f"commits/{revision}")
            for file in detail.get("files", []):
                filename = file["filename"]
                prefix = f"github://{self.repository}/blob/{filename}?"
                if file.get("status") == "removed":
                    self.revoke_source_prefix(prefix, identity)
                    continue
                payload = self._request(
                    "contents/" + urllib.parse.quote(filename, safe="/") + f"?ref={revision}"
                )
                if payload.get("encoding") != "base64" or not payload.get("content"):
                    continue
                raw = base64.b64decode(payload["content"])
                source = prefix + "revision=" + urllib.parse.quote(revision)
                raw_key = self._raw_key(identity, revision, raw)
                if not self.raw_store.put_object(raw_key, raw, "application/octet-stream"):
                    raise RuntimeError(f"could not land Git object: {filename}")
                item_id = self._record_ingest_item(
                    run_id, source, revision, raw_key, hashlib.sha256(raw).hexdigest(), identity
                )
                document, chunker, rejection = prepare_git_document(
                    filename,
                    raw,
                    source,
                    {
                        "source_version": revision,
                        "path": filename,
                        "acl": acl,
                        "raw_object_key": raw_key,
                    },
                )
                if rejection:
                    self._set_ingest_state(item_id, "rejected", identity, rejection=rejection)
                    rejected += 1
                else:
                    assert document is not None and chunker is not None
                    documents.append((document, chunker, item_id))
        return documents, rejected

    def _raw_key(self, identity: dict[str, str], revision: str, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        return f"raw/git/{identity['tenant_id']}/{self.repository}/{revision}/{digest}"

    def _record_ingest_item(
        self,
        run_id: str,
        source: str,
        revision: str,
        raw_key: str,
        content_hash: str,
        identity: dict[str, str],
    ) -> str:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO connector_ingest_items "
                    "(item_id, run_id, connector_id, tenant_id, source_uri, source_version, "
                    "raw_object_key, content_hash, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'landed') "
                    "ON CONFLICT (tenant_id, connector_id, source_uri, source_version) DO UPDATE "
                    "SET run_id = EXCLUDED.run_id, raw_object_key = EXCLUDED.raw_object_key, "
                    "content_hash = EXCLUDED.content_hash, state = 'landed', "
                    "rejection_reason = NULL, "
                    "document_id = NULL, updated_at = now() RETURNING item_id",
                    (
                        uuid.uuid4(),
                        run_id,
                        self.connector_id,
                        identity["tenant_id"],
                        source,
                        revision,
                        raw_key,
                        content_hash,
                    ),
                )
                return str(cursor.fetchone()["item_id"])

    def _set_ingest_state(
        self,
        item_id: str,
        state: str,
        identity: dict[str, str],
        document_id: str | None = None,
        rejection: str | None = None,
    ) -> None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE connector_ingest_items SET state = %s, document_id = %s, "
                    "rejection_reason = %s, updated_at = now() WHERE item_id = %s",
                    (state, document_id, rejection, item_id),
                )

    def sync(
        self,
        identity: dict[str, str],
        vector_store: VectorStore | None = None,
        acl: list[tuple[str, str]] | None = None,
        runs_dir: str | None = None,
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
            documents, rejected = (
                self._documents(commits, acl or [], identity, run_id) if vector_store else ([], 0)
            )
            document_ids = []
            for document, chunker, item_id in documents:
                stored = vector_store.add_documents([document], identity, chunker)
                vector_store.replace_acl(stored, acl or [], identity)
                self._set_ingest_state(item_id, "indexed", identity, document_id=stored[0])
                document_ids.extend(stored)
            for document, _, _ in documents:
                self.revoke_source_prefix(
                    document["source"].split("?", 1)[0] + "?", identity, document["source"]
                )
        except Exception as error:
            with self.database.transaction(identity) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE connector_runs SET state = 'failed', error_summary = %s, "
                        "completed_at = now() WHERE run_id = %s",
                        (str(error)[:500], run_id),
                    )
            self.audit.record(
                identity,
                "connector.sync",
                "connector",
                outcome="failed",
                resource_id=self.connector_id,
                correlation_id=run_id,
                metadata={"error": str(error)[:500]},
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
        result = {
            "run_id": run_id,
            "commit_count": len(commits),
            "document_count": len(document_ids),
            "rejected_count": rejected,
            "cursor": after,
        }
        if runs_dir:
            publish_run(
                runs_dir,
                run_id,
                {
                    "connector": self.connector_id,
                    "cursor_before": before,
                    "cursor_after": after,
                    "document_count": len(document_ids),
                    "state": "succeeded",
                },
            )
        self.audit.record(
            identity,
            "connector.sync",
            "connector",
            resource_id=self.connector_id,
            correlation_id=run_id,
            metadata={"commit_count": len(commits), "document_count": len(document_ids)},
        )
        return result

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

    def revoke_source_prefix(
        self, source_prefix: str, identity: dict[str, str], keep_source: str | None = None
    ) -> int:
        """Retire stale Git revisions after the next version has been stored."""
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                condition = "" if keep_source is None else " AND source_uri <> %s"
                values = (
                    (source_prefix + "%",)
                    if keep_source is None
                    else (source_prefix + "%", keep_source)
                )
                cursor.execute(
                    "SELECT document_id FROM documents WHERE source_uri LIKE %s "
                    "AND status = 'ready'" + condition,
                    values,
                )
                document_ids = [row["document_id"] for row in cursor.fetchall()]
                cursor.execute(
                    "UPDATE documents SET status = 'deleted', deleted_at = now() "
                    "WHERE source_uri LIKE %s AND status = 'ready'" + condition,
                    values,
                )
                if document_ids:
                    cursor.execute(
                        "DELETE FROM document_chunks WHERE document_id = ANY(%s)", (document_ids,)
                    )
        return len(document_ids)
