"""Local, secret-free run receipts for the single-script entrypoint."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_receipt(root: Path, run_id: str, receipt: dict[str, Any]) -> Path:
    target = root / "data" / "runs" / str(run_id) / "receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode()
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".receipt-", delete=False) as handle:
        handle.write(body)
        temporary = Path(handle.name)
    os.replace(temporary, target)
    return target
