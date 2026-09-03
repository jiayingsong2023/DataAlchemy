import os
import uuid

import pytest

from src.harness.revocation_rehearsal import run

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def test_isolated_revocation_rehearsal(monkeypatch):
    monkeypatch.setenv("BUILD_GIT_SHA", "test-build")
    report = run(os.environ["TEST_DATABASE_URL"], f"rtd3-rehearsal-test-{uuid.uuid4()}")

    assert report["decision"] == "PASS"
    assert report["split_contamination"] == 0
    assert all(item["new_adapter_blocked"] for item in report["propagation"].values())
    assert all(item["release_repromotion_blocked"] for item in report["propagation"].values())
