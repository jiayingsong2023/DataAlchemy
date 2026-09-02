"""Immutable feedback source records."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from utils.s3_utils import S3Utils


def save_feedback(
    store: S3Utils,
    query: str,
    answer: str,
    feedback: str = "unrated",
    *,
    owner: str | None = None,
    tenant_id: str = "default",
    run_id: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    retrieval_report: dict[str, Any] | None = None,
    model_execution: dict[str, Any] | None = None,
    answer_policy_version: str = "rag-answer-v1",
) -> str:
    """Write one immutable feedback source object and return its object name."""
    now = datetime.now(timezone.utc)
    filename = f"feedback_{now:%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex}.json"
    body = json.dumps(
        {
            "query": query,
            "answer": answer,
            "feedback": feedback,
            "review_status": "unrated",
            "owner": owner,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "citations": citations or [],
            "retrieval_report": retrieval_report or {},
            "model_execution": model_execution or {},
            "answer_policy_version": answer_policy_version,
            "timestamp": now.isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if not store.put_object(f"feedback/{filename}", body, "application/json"):
        raise RuntimeError("feedback_source_write_failed")
    return filename


def rate_feedback(
    store: S3Utils,
    evaluation: Any,
    identity: dict[str, str],
    feedback_id: str,
    rating: str,
) -> str:
    """Persist one immutable rating and index it in the PostgreSQL authority."""
    if rating not in {"good", "bad"}:
        raise ValueError("feedback_rating_invalid")
    source = store.get_object_body(f"feedback/{feedback_id}")
    if source is None:
        raise FileNotFoundError("feedback_not_found")
    data = json.loads(source)
    if data.get("owner") != identity["username"] or data.get("tenant_id") != identity["tenant_id"]:
        raise FileNotFoundError("feedback_not_found")
    if not data.get("run_id"):
        raise ValueError("feedback_run_id_missing")
    rated = {**data, "feedback": rating, "rated_at": datetime.now(timezone.utc).isoformat()}
    body = json.dumps(rated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    key = f"feedback/ratings/{feedback_id}/{uuid.uuid4().hex}.json"
    if not store.put_object(key, body, "application/json"):
        raise RuntimeError("feedback_rating_write_failed")
    evidence_refs = [
        {
            "span_ids": citation.get("source_span_ids", []),
            "source_version": citation.get("source_version"),
            "content_sha256": citation.get("source_content_sha256"),
            "locator": citation.get("locator"),
        }
        for citation in rated.get("citations", [])
        if citation.get("source_span_ids") and citation.get("source_content_sha256")
    ]
    acl_digests = sorted(
        {citation["acl_digest"] for citation in rated.get("citations", []) if citation.get("acl_digest")}
    )
    return evaluation.create_annotation(
        identity,
        run_id=rated["run_id"],
        trial_id=None,
        kind="user_feedback",
        label={
            "feedback_id": feedback_id,
            "feedback": rating,
            "query": rated.get("query", ""),
            "answer": rated.get("answer", ""),
            "citations": rated.get("citations", []),
            "evidence_refs": evidence_refs,
            "retrieval_report": rated.get("retrieval_report", {}),
            "model_execution": rated.get("model_execution", {}),
            "answer_policy_version": rated.get("answer_policy_version"),
        },
        content_key=key,
        content_sha256=hashlib.sha256(body).hexdigest(),
        source_acl_digest=(
            acl_digests[0]
            if len(acl_digests) == 1
            else hashlib.sha256("\n".join(acl_digests).encode()).hexdigest()
            if acl_digests
            else None
        ),
    )
