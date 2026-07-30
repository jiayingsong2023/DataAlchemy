"""Atomic, hash-verified run manifests for pilot artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def publish_run(root: str | Path, run_id: str, manifest: dict[str, Any]) -> Path:
    root = Path(root)
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    target = run_dir / "manifest.json"
    target.write_bytes(encoded)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise RuntimeError("manifest hash verification failed")
    current = root / "current"
    temporary = root / ".current.tmp"
    temporary.write_text(run_id, encoding="utf-8")
    os.replace(temporary, current)
    return target
