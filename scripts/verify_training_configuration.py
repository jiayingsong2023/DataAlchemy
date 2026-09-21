"""Replay EC4 from persisted evidence using a SELECT-only verifier database role."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verifiers import ReadOnlyServices, default_verifiers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()
    identity = {"tenant_id": args.tenant_id, "username": "ec4-replay", "role": "admin"}
    services = ReadOnlyServices(os.environ["VERIFIER_DATABASE_URL"], identity)
    result = (
        default_verifiers()
        .get("verify_training_configuration", 1)
        .handler({"parameters": {"adapter_id": args.adapter_id}}, identity, {}, services)
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
