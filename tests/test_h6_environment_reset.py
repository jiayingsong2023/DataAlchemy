from pathlib import Path

import pytest

from scripts.reset_pilot_environment import load_environment, reset_plan


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
        "    kube_context: test\n    cluster: test\n    namespace: test\n    helm_release: test\n"
        "    postgres_database: production\n    minio_bucket: test\n    minio_prefix: test/\n"
        "    redis_prefix: 'test:'\n    restore_destination: restore\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="environment_target_forbidden"):
        load_environment(registry, "production")
