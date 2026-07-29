"""Append-only audit records for cloud-model requests."""

import json
import os
from datetime import datetime, timezone
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
