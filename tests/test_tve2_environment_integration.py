import hashlib
import os
from pathlib import Path

import pytest

from scripts.reset_pilot_environment import (
    KubernetesMinioStore,
    build_environment_receipt,
    execute_cleanup,
    execute_reset,
    load_environment,
    preflight_environment,
    reset_plan,
    runtime_image_digest,
)
from src.harness.experience import (
    finalize_environment_receipt,
    publish_environment_receipt,
    publish_rag_task_bundle,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TVE2_RESET_INTEGRATION") != "1",
    reason="destructive isolated TVE-2 integration requires explicit opt-in",
)


def test_real_environment_resets_are_stable_isolated_and_cleanup():
    registry = Path("deploy/pilot-environments.example.yaml")
    environment = load_environment(registry, "dataalchemy-gpu-test")
    plan = reset_plan(environment)
    expected = f"reset:{environment['environment_id']}:{plan['plan_sha256'][:12]}"
    assert os.getenv("TVE2_RESET_CONFIRM") == expected

    store = KubernetesMinioStore(environment)
    task_environment = {
        "schema_version": "tve_pdf_environment.v1",
        "environment_id": environment["environment_id"],
        "tenant_id": environment["tenant_id"],
        "fixture_sha256": environment["fixture_sha256"],
        "source_permission_active": True,
    }
    reset_script_sha256 = hashlib.sha256(
        Path("scripts/reset_pilot_environment.py").read_bytes()
    ).hexdigest()
    assets = publish_rag_task_bundle(
        store,
        {
            "case_id": "tve2-real-reset",
            "query": "令狐冲转生后变成了什么？",
            "input_sha256": "0" * 64,
            "expected_status": "grounded",
            "required_substrings": ["史莱姆"],
            "required_pages": [1],
        },
        tenant_id=environment["tenant_id"],
        environment_snapshot=task_environment,
        reset_contract={
            "kind": "registered-script",
            "ref": "scripts/reset_pilot_environment.py",
            "sha256": reset_script_sha256,
        },
        tool_contract={
            "name": "pdf_rag_fixture",
            "version": 1,
            "contract_sha256": reset_script_sha256,
        },
        verifier_name="verify_environment",
        verifier_version=1,
        limits={"max_steps": 1, "deadline_seconds": 300},
        acl_sha256=hashlib.sha256(environment["tenant_id"].encode()).hexdigest(),
        permission_version="tve2-real-v1",
        retention_until="2027-08-23T00:00:00Z",
    )
    registry_sha256 = hashlib.sha256(registry.read_bytes()).hexdigest()
    image_digest = runtime_image_digest(environment)
    initial_states = []
    receipts = []
    for _ in range(3):
        reset = execute_reset(environment, plan)
        checks, observations = preflight_environment(environment, task_environment)
        receipt, preflight = build_environment_receipt(
            environment,
            tenant_id=environment["tenant_id"],
            task_bundle_id=assets["fingerprint"]["task_bundle_id"],
            registry_sha256=registry_sha256,
            reset=reset,
            fixture_sha256=environment["fixture_sha256"],
            image_digest=image_digest,
            tool_contracts_sha256=reset_script_sha256,
            checks=checks,
            observations=observations,
        )
        assert receipt["state"] == "ready"
        publish_environment_receipt(store, receipt, preflight, tenant_id=environment["tenant_id"])
        initial_states.append(receipt["initial_state_sha256"])
        receipts.append((receipt, preflight))

    assert len(set(initial_states)) == 1
    revoked_checks, _ = preflight_environment(
        environment, {**task_environment, "source_permission_active": False}
    )
    assert revoked_checks["source_permission_active"] is False

    cleanup = execute_cleanup(environment)
    assert cleanup == {"status": "completed", "error_code": None}
    final = finalize_environment_receipt(
        receipts[-1][0],
        {"fixture_removed": True, "model_side_effects": []},
        cleanup_status=cleanup["status"],
    )
    published = publish_environment_receipt(
        store, final, receipts[-1][1], tenant_id=environment["tenant_id"]
    )
    assert store.get(published["receipt_ref"])
