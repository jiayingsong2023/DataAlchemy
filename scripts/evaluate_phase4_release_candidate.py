"""Exercise two governed release cycles: promotion and rollback."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.release.governance import ReleaseGovernance


def main() -> None:
    identity = {
        "tenant_id": f"phase4-release-{uuid.uuid4()}",
        "username": "evaluator",
        "role": "admin",
    }
    releases = ReleaseGovernance(os.environ["DATABASE_URL"])
    manifest = {
        "code_version": "phase4",
        "evaluation": {"passed": True},
        "rollback_to": "stable",
        "guardrails": {"max_error_rate": 0.01, "max_p95_ms": 1_000},
    }
    promoted = releases.create_candidate(identity, manifest)
    releases.advance(promoted, "shadow", identity)
    releases.advance(promoted, "canary", identity)
    assert releases.advance(promoted, "promoted", identity)["status"] == "promoted"
    rolled_back = releases.create_candidate(identity, manifest)
    releases.advance(rolled_back, "shadow", identity)
    releases.advance(rolled_back, "canary", identity)
    assert releases.observe(rolled_back, {"error_rate": 0.02}, identity) == "rolled_back"
    print('{"suite":"phase4_release_candidate","cycles":2,"promoted":1,"rolled_back":1}')


if __name__ == "__main__":
    main()
