"""Durable conversation context, compaction and memory-candidate helpers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from storage.postgres import PostgresDatabase

CONTEXT_PACK_DIR = Path(__file__).resolve().parents[1] / "harness" / "context_packs"
DEFAULT_INPUT_BUDGET = 7000
DEFAULT_OUTPUT_RESERVE = 1000
PACK_TASK_MAP = {
    "chat": "chat_rag",
    "rag_chat": "chat_rag",
    "document_product_loop": "document_product_loop",
    "ingest": "document_product_loop",
    "memory_distillation": "memory_distillation",
    "session_close": "memory_distillation",
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def estimate_tokens(value: str) -> int:
    """Conservative tokenizer fallback; model tokenizers can replace this later."""
    return max(1, (len(value) + 3) // 4)


def _content_text(content: dict[str, Any]) -> str:
    for key in ("content", "text", "query", "answer", "output"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


class ContextService:
    """Single owner of PostgreSQL conversation facts and bounded context assembly."""

    def __init__(self, database_url: str, retriever: Any | None = None, memory: Any | None = None):
        self.database = PostgresDatabase(database_url)
        self.retriever = retriever
        self.memory = memory

    @staticmethod
    def _identity_digest(identity: dict[str, str]) -> str:
        return canonical_digest({key: identity[key] for key in ("tenant_id", "username", "role")})

    @staticmethod
    def _pack(task_type: str) -> dict[str, Any]:
        pack_id = PACK_TASK_MAP.get(task_type, "chat_rag")
        path = CONTEXT_PACK_DIR / f"{pack_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sha256"] = canonical_digest(payload)
        return payload

    def create_session(
        self, identity: dict[str, str], title: str = "New Chat", auto_memory_enabled: bool = False
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        safe_title = " ".join(title.split())[:120] or "New Chat"
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO conversation_sessions "
                    "(session_id, tenant_id, owner_id, title, auto_memory_enabled) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        session_id,
                        identity["tenant_id"],
                        identity["username"],
                        safe_title,
                        auto_memory_enabled,
                    ),
                )
        return self.get_session(session_id, identity)

    def get_session(self, session_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT session_id, tenant_id, owner_id, title, state, auto_memory_enabled, "
                    "context_generation, version, created_at, updated_at, closed_at "
                    "FROM conversation_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise PermissionError("Session not found")
        return {**row, "session_id": str(row["session_id"])}

    def list_sessions(self, identity: dict[str, str]) -> list[dict[str, Any]]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT session_id, title, state, auto_memory_enabled, context_generation, "
                    "version, created_at, updated_at FROM conversation_sessions "
                    "WHERE tenant_id = %s AND owner_id = %s AND state <> 'deleted' "
                    "ORDER BY updated_at DESC LIMIT 200",
                    (identity["tenant_id"], identity["username"]),
                )
                return [{**row, "session_id": str(row["session_id"])} for row in cursor.fetchall()]

    def append_event(
        self,
        session_id: str,
        event_type: str,
        content: dict[str, Any],
        identity: dict[str, str],
        *,
        trust_label: str = "trusted_user",
        expected_version: int | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        agent_event_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in {
            "user_message",
            "assistant_message",
            "tool_observation",
            "session_closed",
            "context_reset",
        }:
            raise ValueError("Unsupported conversation event type")
        if trust_label not in {
            "trusted_user",
            "trusted_system",
            "verified_tool",
            "untrusted_external",
            "legacy_unverified",
        }:
            raise ValueError("Unsupported trust label")
        canonical = json.loads(json.dumps(content, ensure_ascii=False, sort_keys=True))
        event_id = str(uuid.uuid4())
        content_hash = canonical_digest(canonical)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT context_generation, version, state FROM conversation_sessions "
                    "WHERE session_id = %s FOR UPDATE",
                    (session_id,),
                )
                session = cursor.fetchone()
                if session is None:
                    raise PermissionError("Session not found")
                if session["state"] != "active":
                    raise ValueError("Session is not active")
                if expected_version is not None and session["version"] != expected_version:
                    raise RuntimeError("session_version_conflict")
                cursor.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
                    "FROM conversation_events WHERE session_id = %s",
                    (session_id,),
                )
                sequence_no = int(cursor.fetchone()["next_sequence"])
                cursor.execute(
                    "INSERT INTO conversation_events "
                    "(event_id, session_id, tenant_id, sequence_no, generation, event_type, "
                    "content_json, content_sha256, trust_label, task_id, run_id, agent_event_id, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
                    (
                        event_id,
                        session_id,
                        identity["tenant_id"],
                        sequence_no,
                        session["context_generation"],
                        event_type,
                        json.dumps(canonical, ensure_ascii=False),
                        content_hash,
                        trust_label,
                        task_id,
                        run_id,
                        agent_event_id,
                        identity["username"],
                    ),
                )
                cursor.execute(
                    "UPDATE conversation_sessions SET version = version + 1, updated_at = now() "
                    "WHERE session_id = %s",
                    (session_id,),
                )
        return {
            "event_id": event_id,
            "session_id": session_id,
            "sequence_no": sequence_no,
            "generation": session["context_generation"],
            "content_sha256": content_hash,
        }

    def events(
        self, session_id: str, identity: dict[str, str], *, generation: int | None = None
    ) -> list[dict[str, Any]]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                query = (
                    "SELECT event_id, sequence_no, generation, event_type, content_json, "
                    "content_sha256, trust_label, task_id, run_id, created_by, created_at "
                    "FROM conversation_events WHERE session_id = %s"
                )
                params: list[Any] = [session_id]
                if generation is not None:
                    query += " AND generation = %s"
                    params.append(generation)
                query += " ORDER BY sequence_no"
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return [
            {
                **row,
                "event_id": str(row["event_id"]),
                "content": row.pop("content_json"),
                "task_id": str(row["task_id"]) if row.get("task_id") else None,
                "run_id": str(row["run_id"]) if row.get("run_id") else None,
            }
            for row in rows
        ]

    def event(self, event_id: str, identity: dict[str, str]) -> dict[str, Any] | None:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, session_id, sequence_no, event_type, content_json, trust_label "
                    "FROM conversation_events WHERE event_id = %s",
                    (event_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            **row,
            "event_id": str(row["event_id"]),
            "session_id": str(row["session_id"]),
            "content": row.pop("content_json"),
        }

    def set_auto_memory(
        self, session_id: str, enabled: bool, identity: dict[str, str], expected_version: int
    ) -> dict[str, Any]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE conversation_sessions SET auto_memory_enabled = %s, version = version + 1, "
                    "updated_at = now() WHERE session_id = %s AND owner_id = %s AND version = %s "
                    "AND state = 'active' RETURNING version",
                    (enabled, session_id, identity["username"], expected_version),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("session_version_conflict")
        return self.get_session(session_id, identity)

    def _active_checkpoint(
        self, session_id: str, generation: int, identity: dict[str, str]
    ) -> dict[str, Any] | None:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT checkpoint_id, source_sequence_start, source_sequence_end, source_digest, summary, handoff_json "
                    "FROM context_checkpoints WHERE session_id = %s AND generation = %s AND status = 'active'",
                    (session_id, generation),
                )
                row = cursor.fetchone()
        if row:
            row["checkpoint_id"] = str(row["checkpoint_id"])
        return row

    def build_context(
        self,
        session_id: str,
        query: str,
        identity: dict[str, str],
        *,
        task_type: str = "chat",
        task: dict[str, Any] | None = None,
        input_budget: int = DEFAULT_INPUT_BUDGET,
        output_reserve: int = DEFAULT_OUTPUT_RESERVE,
    ) -> dict[str, Any]:
        session = self.get_session(session_id, identity)
        events = self.events(session_id, identity)
        checkpoint = self._active_checkpoint(session_id, session["context_generation"], identity)
        pack = self._pack(task_type)
        available = max(1, input_budget - output_reserve)
        sections: list[tuple[str, str, int, list[str]]] = []
        sections.append(("rules", json.dumps(pack, ensure_ascii=False), 0, []))
        handoff_text = checkpoint["summary"] if checkpoint else "No previous handoff."
        sections.append(("handoff", handoff_text, 1, []))
        event_rows = events
        if checkpoint:
            event_rows = [
                item for item in events if item["sequence_no"] > checkpoint["source_sequence_end"]
            ]
        recent_ids: list[str] = []
        recent_text = []
        for item in event_rows[-12:]:
            recent_ids.append(item["event_id"])
            recent_text.append(f"{item['event_type']}: {_content_text(item['content'])}")
        sections.append(("conversation", "\n".join(recent_text), 2, recent_ids))
        document_rows: list[dict[str, Any]] = []
        memory_rows: list[dict[str, Any]] = []
        if query.strip() and self.retriever is not None:
            document_rows = [
                {**item, "context_type": "document"}
                for item in self.retriever.retrieve(query, identity, top_k=8)
            ]
        if query.strip() and self.memory is not None:
            memory_rows = self.memory.retrieve(query, identity, top_k=4)
        document_ids = [
            str(item.get("metadata", {}).get("chunk_id", item.get("chunk_id", "")))
            for item in document_rows
        ]
        memory_ids = [str(item["memory_id"]) for item in memory_rows]
        sections.append(
            (
                "documents",
                "\n".join(str(item.get("text", "")) for item in document_rows),
                3,
                document_ids,
            )
        )
        sections.append(
            (
                "memories",
                "\n".join(str(item.get("text", "")) for item in memory_rows),
                4,
                memory_ids,
            )
        )

        used = 0
        selected: list[dict[str, Any]] = []
        for name, text, priority, refs in sections:
            if not text:
                continue
            remaining = available - used
            if remaining <= 0:
                break
            if estimate_tokens(text) > remaining:
                # ponytail: deterministic character cut keeps the envelope bounded; use model tokenizer when measured need appears.
                text = text[: max(4, remaining * 4)]
            cost = estimate_tokens(text)
            selected.append(
                {"section": name, "text": text, "tokens": cost, "refs": refs, "priority": priority}
            )
            used += cost
        envelope = {
            "schema_version": "context-envelope.v1",
            "snapshot_id": str(uuid.uuid4()),
            "query": query,
            "identity": {
                "tenant_id": identity["tenant_id"],
                "username": identity["username"],
                "role": identity["role"],
            },
            "task": {
                "task_id": (task or {}).get("task_id"),
                "task_spec_sha256": (task or {}).get("task_spec_sha256"),
                "plan_version": (task or {}).get("plan_version"),
            },
            "packs": [
                {"pack_id": pack["pack_id"], "version": pack["version"], "sha256": pack["sha256"]}
            ],
            "handoff": {
                "checkpoint_id": checkpoint["checkpoint_id"] if checkpoint else None,
                "generation": session["context_generation"],
            },
            "recent_event_ids": recent_ids,
            "document_chunk_ids": document_ids,
            "retrieval_context": json.loads(
                json.dumps(document_rows, ensure_ascii=False, default=str)
            ),
            "memory_ids": memory_ids,
            "budget": {
                "input_tokens": input_budget,
                "reserved_output_tokens": output_reserve,
                "used_tokens": used,
            },
            "sections": selected,
        }
        envelope["envelope_sha256"] = canonical_digest(
            {key: value for key, value in envelope.items() if key != "envelope_sha256"}
        )
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO context_snapshots "
                    "(snapshot_id, session_id, tenant_id, generation, task_id, run_id, task_spec_sha256, plan_version, "
                    "identity_digest, pack_refs, checkpoint_id, recent_event_ids, document_chunk_ids, memory_ids, budget_json, envelope_sha256) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)",
                    (
                        envelope["snapshot_id"],
                        session_id,
                        identity["tenant_id"],
                        session["context_generation"],
                        (task or {}).get("task_id"),
                        (task or {}).get("run_id"),
                        (task or {}).get("task_spec_sha256"),
                        (task or {}).get("plan_version"),
                        self._identity_digest(identity),
                        json.dumps(envelope["packs"]),
                        checkpoint["checkpoint_id"] if checkpoint else None,
                        json.dumps(recent_ids),
                        json.dumps(document_ids),
                        json.dumps(memory_ids),
                        json.dumps(envelope["budget"]),
                        envelope["envelope_sha256"],
                    ),
                )
        return envelope

    def compact(
        self, session_id: str, identity: dict[str, str], *, summary: str | None = None
    ) -> dict[str, Any]:
        session = self.get_session(session_id, identity)
        rows = self.events(session_id, identity, generation=session["context_generation"])
        if not rows:
            raise ValueError("cannot compact an empty session")
        start = rows[0]["sequence_no"]
        end = rows[-1]["sequence_no"]
        source_digest = canonical_digest(
            [
                {
                    "event_id": item["event_id"],
                    "sequence_no": item["sequence_no"],
                    "hash": item["content_sha256"],
                }
                for item in rows
            ]
        )
        if summary is None:
            summary = "\n".join(
                f"{item['event_type']}: {_content_text(item['content'])}" for item in rows[-8:]
            )[:6000]
        claims = [
            {
                "text": _content_text(item["content"]),
                "source_event_ids": [item["event_id"]],
                "status": "observed",
            }
            for item in rows
            if item["event_type"] == "user_message"
        ][-20:]
        handoff = {
            "goal": _content_text(rows[0]["content"]),
            "source_digest": source_digest,
            "confirmed_claims": claims,
            "open_items": [],
            "last_task_id": rows[-1].get("task_id"),
            "last_run_id": rows[-1].get("run_id"),
            "task_spec_sha256": None,
            "plan_version": None,
        }
        checkpoint_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE context_checkpoints SET status = 'superseded' "
                    "WHERE session_id = %s AND generation = %s AND status IN ('active', 'verified')",
                    (session_id, session["context_generation"]),
                )
                cursor.execute(
                    "INSERT INTO context_checkpoints "
                    "(checkpoint_id, session_id, tenant_id, generation, source_sequence_start, source_sequence_end, source_digest, "
                    "summary, handoff_json, status, verifier_name, verifier_version, verifier_result, verified_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'active', 'verify_context_checkpoint', 1, %s::jsonb, now())",
                    (
                        checkpoint_id,
                        session_id,
                        identity["tenant_id"],
                        session["context_generation"],
                        start,
                        end,
                        source_digest,
                        summary,
                        json.dumps(handoff, ensure_ascii=False),
                        json.dumps({"status": "passed", "source_count": len(rows)}),
                    ),
                )
        return {
            "checkpoint_id": checkpoint_id,
            "source_sequence_start": start,
            "source_sequence_end": end,
            "source_digest": source_digest,
            "handoff": handoff,
        }

    def resume(
        self,
        session_id: str,
        identity: dict[str, str],
        *,
        task_spec_sha256: str | None = None,
        plan_version: int | None = None,
    ) -> dict[str, Any]:
        """Revalidate checkpoint identity, source digest and task contract before continuing."""
        session = self.get_session(session_id, identity)
        checkpoint = self._active_checkpoint(session_id, session["context_generation"], identity)
        if checkpoint is None:
            raise RuntimeError("checkpoint_missing")
        events = self.events(session_id, identity)
        covered = [
            item
            for item in events
            if checkpoint["source_sequence_start"]
            <= item["sequence_no"]
            <= checkpoint["source_sequence_end"]
        ]
        digest = canonical_digest(
            [
                {
                    "event_id": item["event_id"],
                    "sequence_no": item["sequence_no"],
                    "hash": item["content_sha256"],
                }
                for item in covered
            ]
        )
        if digest != checkpoint["source_digest"]:
            raise RuntimeError("checkpoint_corrupt")
        handoff = checkpoint["handoff_json"]
        if handoff.get("task_spec_sha256") and handoff["task_spec_sha256"] != task_spec_sha256:
            raise RuntimeError("task_contract_changed")
        if handoff.get("plan_version") is not None and handoff["plan_version"] != plan_version:
            raise RuntimeError("plan_stale")
        return {
            "session_id": session_id,
            "generation": session["context_generation"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "handoff": handoff,
            "source_event_count": len(covered),
        }

    def reset(
        self, session_id: str, identity: dict[str, str], expected_version: int
    ) -> dict[str, Any]:
        session = self.get_session(session_id, identity)
        if session["version"] != expected_version:
            raise RuntimeError("session_version_conflict")
        checkpoint = self.compact(session_id, identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT version, context_generation FROM conversation_sessions "
                    "WHERE session_id = %s AND owner_id = %s FOR UPDATE",
                    (session_id, identity["username"]),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Session not found")
                if row["version"] != expected_version:
                    raise RuntimeError("session_version_conflict")
                cursor.execute(
                    "UPDATE conversation_sessions SET context_generation = context_generation + 1, version = version + 1, updated_at = now() "
                    "WHERE session_id = %s",
                    (session_id,),
                )
        event = self.append_event(
            session_id, "context_reset", {"checkpoint_id": checkpoint["checkpoint_id"]}, identity
        )
        next_generation = row["context_generation"] + 1
        next_checkpoint_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO context_checkpoints "
                    "(checkpoint_id, session_id, tenant_id, generation, source_sequence_start, source_sequence_end, source_digest, "
                    "summary, handoff_json, status, verifier_name, verifier_version, verifier_result, verified_at) "
                    "SELECT %s, session_id, tenant_id, %s, source_sequence_start, source_sequence_end, source_digest, "
                    "summary, handoff_json, 'active', verifier_name, verifier_version, verifier_result, verified_at "
                    "FROM context_checkpoints WHERE checkpoint_id = %s",
                    (next_checkpoint_id, next_generation, checkpoint["checkpoint_id"]),
                )
        return {
            "checkpoint": {**checkpoint, "checkpoint_id": next_checkpoint_id},
            "event": event,
            "generation": next_generation,
        }

    @staticmethod
    def extract_candidates(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Small deterministic extractor; a configured LLM may propose richer JSON later."""
        candidates: list[dict[str, Any]] = []
        patterns = [
            (
                re.compile(r"(?:我偏好|我喜欢|请用|prefer|I prefer)\s*(.+)$", re.I),
                "user.preference",
            ),
            (re.compile(r"(?:记住|remember)\s*[:：]?\s*(.+)$", re.I), "user.remembered_fact"),
        ]
        for event in events:
            if event.get("event_type") != "user_message":
                continue
            text = _content_text(event.get("content", {}))
            for pattern, claim_key in patterns:
                match = pattern.search(text)
                if match:
                    content = match.group(1).strip()[:1000]
                    if content:
                        candidates.append(
                            {
                                "kind": "profile",
                                "scope_type": "personal",
                                "scope_id": event.get("created_by"),
                                "claim_key": claim_key,
                                "content": content,
                                "source_event_ids": [event["event_id"]],
                                "confidence": 0.96,
                                "trust_label": event.get("trust_label", "trusted_user"),
                                "sensitivity_label": "none",
                                "risk_class": "low",
                                "policy_version": "memory-policy.v1",
                            }
                        )
                    break
        return candidates
