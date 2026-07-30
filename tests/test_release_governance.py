import os
import uuid

import pytest

from src.release.governance import ReleaseGovernance

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity(role="admin"):
    return {"tenant_id": f"release-{uuid.uuid4()}", "username": "alice", "role": role}


def test_release_requires_evidence_and_supports_governed_rollback():
    owner = identity()
    releases = ReleaseGovernance(os.environ["TEST_DATABASE_URL"])
    with pytest.raises(ValueError):
        releases.create_candidate(owner, {})
    with pytest.raises(ValueError, match="evaluation"):
        releases.create_candidate(
            owner, {"code_version": "abc", "evaluation": {}, "rollback_to": "stable"}
        )
    manifest = {
        "code_version": "abc",
        "evaluation": {"passed": True},
        "rollback_to": "stable",
        "guardrails": {"max_error_rate": 0.01, "max_p95_ms": 100},
    }
    release_id = releases.create_candidate(owner, manifest)
    assert releases.advance(release_id, "shadow", owner)["status"] == "shadow"
    assert releases.advance(release_id, "canary", owner)["status"] == "canary"
    assert releases.observe(release_id, {"error_rate": 0.02}, owner) == "rolled_back"
    with pytest.raises(PermissionError):
        releases.create_candidate(
            {**owner, "role": "user"},
            {"code_version": "x", "evaluation": {}, "rollback_to": "x"},
        )
