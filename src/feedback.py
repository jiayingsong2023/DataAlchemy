"""Immutable feedback source records."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

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
