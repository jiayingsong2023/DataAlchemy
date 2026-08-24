"""Append-only audit records for cloud-model requests."""

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from config import CLOUD_AUDIT_PATH


def record_cloud_call(component: str, model: str, fields: list[str]) -> str:
    run_id = str(uuid4())
    os.makedirs(os.path.dirname(CLOUD_AUDIT_PATH), exist_ok=True)
    record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "model": model,
        "fields": fields,
    }
    with open(CLOUD_AUDIT_PATH, "a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return run_id


def observable_model_call(
    *,
    component: str,
    model: str,
    messages: list[dict[str, str]],
    response: str | None,
    generation_config: dict[str, Any],
    latency_ms: float,
    status: str,
    revision_or_digest: str | None = None,
    tokenizer_sha256: str | None = None,
    chat_template_sha256: str | None = None,
    usage: dict[str, Any] | None = None,
    provider_request_id: str | None = None,
) -> dict[str, Any]:
    """Build the observable model-call contract; never infer unavailable telemetry."""

    def optional(value: Any, reason: str) -> dict[str, Any]:
        return {"value": value, "unavailable_reason": None if value is not None else reason}

    return {
        "schema_version": "observable_model_call.v1",
        "component": component,
        "request": {"messages": messages},
        "response": {"content": response},
        "status": status,
        "model": {
            "id": model,
            "revision_or_digest": optional(revision_or_digest, "model_revision_not_exposed"),
            "tokenizer_sha256": optional(tokenizer_sha256, "tokenizer_digest_not_exposed"),
            "chat_template_sha256": optional(
                chat_template_sha256, "chat_template_digest_not_exposed"
            ),
        },
        "generation_config": generation_config,
        "usage": optional(usage, "provider_usage_not_exposed"),
        "latency_ms": latency_ms,
        "provider_request_id": optional(provider_request_id, "provider_request_id_not_exposed"),
        "token_ids": optional(None, "runtime_token_ids_not_captured"),
        "logprobs": optional(None, "runtime_logprobs_not_exposed"),
    }
