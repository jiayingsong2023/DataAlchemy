import os
import uuid

import pytest

from src.storage.audit import AuditLog

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def identity(tenant_id=None, role="admin"):
    return {"tenant_id": tenant_id or f"audit-{uuid.uuid4()}", "username": "alice", "role": role}


def test_audit_log_redacts_secrets_and_enforces_tenant_admin_scope():
    log = AuditLog(os.environ["TEST_DATABASE_URL"])
    owner = identity()
    log.record(
        owner,
        "tool.call",
        "tool",
        resource_id="sync_git",
        correlation_id="task-1",
        metadata={"token": "do-not-store", "count": 1},
    )
    event = log.list(owner)[0]
    assert event["metadata_json"] == {"token": "***", "count": 1}
    with pytest.raises(PermissionError):
        log.list({**owner, "role": "user"})
    assert log.list(identity()) == []
