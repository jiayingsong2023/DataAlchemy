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
        },
        content_key=key,
        content_sha256=hashlib.sha256(body).hexdigest(),
    )
