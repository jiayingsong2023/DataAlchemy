"""Durable HTTP/WebSocket request bindings; AgentRuntime remains the task authority."""

from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4

from core.evidence import sha256
from storage.postgres import PostgresDatabase


class RequestConflict(RuntimeError):
    pass


class RequestInProgress(RuntimeError):
    pass


class ChatRequests:
    def __init__(self, database: PostgresDatabase):
        self.database = database

    @staticmethod
    def key(request_id: str, session_id: str | None, identity: dict[str, str]) -> str:
        return sha256(
            {
                "request_id": str(UUID(request_id)),
                "session_id": str(UUID(session_id)) if session_id else None,
                "tenant_id": identity["tenant_id"],
                "owner_id": identity["username"],
            }
        )

    def get(self, key: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM chat_requests WHERE request_key = %s", (key,)
            ).fetchone()
        if row is None:
            raise PermissionError("Chat request not found")
        return {
            name: str(value) if isinstance(value, UUID) else value for name, value in row.items()
        }

    @contextmanager
    def claim(
        self, request_id: str, session_id: str | None, query: str, identity: dict[str, str]
    ) -> Iterator[dict[str, Any]]:
        key = self.key(request_id, session_id, identity)
        payload_hash = sha256({"query": query})
        # ponytail: one DB connection held per active request; move to a fenced lease
        # only if measured connection pressure requires it. Never wait on a busy lock.
        with self.database.transaction(identity) as lock_connection:
            locked = lock_connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                (int.from_bytes(bytes.fromhex(key[:16]), "big", signed=True),),
            ).fetchone()["acquired"]
            if not locked:
                raise RequestInProgress("chat_request_in_progress")
            with self.database.transaction(identity) as connection:
                connection.execute(
                    "INSERT INTO chat_requests "
                    "(request_key, request_id, tenant_id, owner_id, payload_sha256, session_id, task_id, run_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (request_key) DO NOTHING",
                    (
                        key,
                        request_id,
                        identity["tenant_id"],
                        identity["username"],
                        payload_hash,
                        session_id or str(uuid4()),
                        str(uuid4()),
                        str(uuid4()),
                    ),
                )
            receipt = self.get(key, identity)
            if receipt["payload_sha256"] != payload_hash:
                raise RequestConflict("chat_request_payload_conflict")
            yield receipt

    def save_context(
        self, receipt: dict[str, Any], ref: str, digest: str, identity: dict[str, str]
    ) -> None:
        with self.database.transaction(identity) as connection:
            row = connection.execute(
                "UPDATE chat_requests SET context_ref = %s, context_sha256 = %s "
                "WHERE request_key = %s AND context_ref IS NULL RETURNING request_key",
                (ref, digest, receipt["request_key"]),
            ).fetchone()
            if row is None:
                raise RequestConflict("chat_request_context_already_bound")
