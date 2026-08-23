import hashlib
from pathlib import Path

import pytest

from scripts.reset_pilot_environment import (
    build_environment_receipt,
    load_environment,
    reset_plan,
)
from src.core.evidence import ObjectNotFound
from src.harness.experience import publish_environment_receipt, validate_environment_receipt


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, body):
        self.objects[key] = body

    def get(self, key):
        try:
            return self.objects[key]
        except KeyError as error:
            raise ObjectNotFound(key) from error


def test_environment_registry_is_allowlisted_and_plan_is_stable():
    environment = load_environment(
        Path("deploy/pilot-environments.example.yaml"),
        "dataalchemy-gpu-test",
    )
    plan = reset_plan(environment)
    assert plan["environment_id"] == "dataalchemy-gpu-test"
    assert len(plan["plan_sha256"]) == 64
    assert "clear_postgres_test_schema" in plan["actions"]


def test_environment_registry_rejects_production_target(tmp_path):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "environments:\n  - environment_id: production\n    type: test\n    reset_allowed: true\n"
        "    tenant_id: pilot-tenant\n"
        "    kube_context: test\n    cluster: test\n    namespace: test\n    helm_release: test\n"
        "    postgres_database: production\n    minio_bucket: test\n    minio_prefix: test/\n"
        "    redis_prefix: 'test:'\n    restore_destination: restore\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="environment_target_forbidden"):
        load_environment(registry, "production")


def test_environment_receipt_is_stable_across_resets_and_published_immutably():
    registry = Path("deploy/pilot-environments.example.yaml")
    environment = load_environment(registry, "dataalchemy-gpu-test")
    plan = reset_plan(environment)
    checks = {
        "services_healthy": True,
        "fixture_present": True,
        "tenant_acl_readable": True,
        "cross_tenant_denied": True,
        "target_matches_bundle": True,
        "source_permission_active": True,
    }
    receipts = [
        build_environment_receipt(
            environment,
            tenant_id="pilot-tenant",
            task_bundle_id="sha256:" + "a" * 64,
            registry_sha256=hashlib.sha256(registry.read_bytes()).hexdigest(),
            reset={
                "receipt_id": f"reset-{number}",
                "plan_sha256": plan["plan_sha256"],
                "status": "reset_complete",
                "error_code": None,
            },
            fixture_sha256="b" * 64,
            image_digest="sha256:" + "c" * 64,
            tool_contracts_sha256="d" * 64,
            checks=checks,
        )
        for number in range(3)
    ]
    assert len({receipt[0]["initial_state_sha256"] for receipt in receipts}) == 1
    receipt, preflight = receipts[0]
    assert validate_environment_receipt(receipt)["state"] == "ready"
    published = publish_environment_receipt(
        MemoryStore(), receipt, preflight, tenant_id="pilot-tenant"
    )
    assert published["receipt_ref"].startswith(
        "tenants/pilot-tenant/environment-evidence/receipts/"
    )
    with pytest.raises(ValueError, match="contract_tenant_mismatch"):
        publish_environment_receipt(
            MemoryStore(),
            receipt,
            {**preflight, "tenant_id": "other"},
            tenant_id="pilot-tenant",
        )


def test_environment_receipt_fails_closed_for_tenant_or_preflight_failure():
    registry = Path("deploy/pilot-environments.example.yaml")
    environment = load_environment(registry, "dataalchemy-gpu-test")
    plan = reset_plan(environment)
    arguments = {
        "task_bundle_id": "sha256:" + "a" * 64,
        "registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
        "reset": {
            "receipt_id": "reset-failed-preflight",
            "plan_sha256": plan["plan_sha256"],
            "status": "reset_complete",
            "error_code": None,
        },
        "fixture_sha256": "b" * 64,
        "image_digest": "sha256:" + "c" * 64,
        "tool_contracts_sha256": "d" * 64,
        "checks": {
            "services_healthy": True,
            "fixture_present": False,
            "tenant_acl_readable": True,
            "cross_tenant_denied": True,
            "target_matches_bundle": True,
            "source_permission_active": True,
        },
    }
    with pytest.raises(ValueError, match="environment_tenant_mismatch"):
        build_environment_receipt(environment, tenant_id="other", **arguments)

    receipt, _ = build_environment_receipt(environment, tenant_id="pilot-tenant", **arguments)
    assert receipt["state"] == "invalidated"
    assert receipt["invalid_reason"] == "fixture_missing"
