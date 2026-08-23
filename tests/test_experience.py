import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.verifiers import VerificationResult, VerifierSpec
from src.harness.experience import (
    project_verification_result,
    task_bundle_id,
    validate_environment_receipt,
    validate_task_bundle,
    validate_verifier_result,
)

FIXTURE = Path(__file__).parent / "fixtures" / "experience" / "tve_contract_cases.json"


def cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def verifier_handler(_criterion, _task, _result, _services):
    return VerificationResult("passed")


def test_task_bundle_is_canonical_and_content_addressed():
    bundle = cases()["valid_task_bundle"]
    assert validate_task_bundle(bundle) == bundle
    assert task_bundle_id(bundle) == task_bundle_id(deepcopy(bundle))

    changed = deepcopy(bundle)
    changed["task"]["input_sha256"] = "1" * 64
    assert task_bundle_id(changed) != task_bundle_id(bundle)


def test_task_bundle_rejects_missing_hash_hidden_answer_secret_and_tenant_drift():
    bundle = cases()["valid_task_bundle"]

    missing_hash = deepcopy(bundle)
    missing_hash["environment"]["snapshot_sha256"] = ""
    with pytest.raises(ValueError, match="task_bundle_environment_hash_invalid"):
        validate_task_bundle(missing_hash)

    hidden_answer = deepcopy(bundle)
    hidden_answer["task"]["expected_answer"] = "not model-visible"
    with pytest.raises(ValueError, match="task_bundle_hidden_answer_forbidden"):
        validate_task_bundle(hidden_answer)

    secret = deepcopy(bundle)
    secret["governance"]["api_key"] = "not-a-real-key"
    with pytest.raises(ValueError, match="contract_secret_field_forbidden"):
        validate_task_bundle(secret)

    tenant_drift = deepcopy(bundle)
    tenant_drift["task"]["input_tenant_id"] = "other-tenant"
    with pytest.raises(ValueError, match="contract_tenant_mismatch"):
        validate_task_bundle(tenant_drift)


def test_environment_receipts_distinguish_ready_from_invalidated():
    payload = cases()
    assert validate_environment_receipt(payload["valid_environment_receipt"])["state"] == "ready"
    invalid = validate_environment_receipt(payload["invalid_environment_receipt"])
    assert invalid["state"] == "invalidated"
    assert invalid["invalid_reason"] == "fixture_missing"

    ambiguous = deepcopy(payload["valid_environment_receipt"])
    ambiguous["state"] = "invalidated"
    ambiguous["invalid_reason"] = "fixture_missing"
    with pytest.raises(ValueError, match="environment_invalid_without_failure"):
        validate_environment_receipt(ambiguous)


def test_verifier_contract_keeps_model_failure_separate_from_invalid_runs():
    results = cases()["verifier_results"]
    for result in results.values():
        validate_verifier_result(result)

    assert results["model_failure"]["trial_state"] == "failed"
    assert results["authorization_failure"]["trial_state"] == "failed"
    assert results["environment_invalid"]["trial_state"] == "invalidated"
    assert results["verifier_invalid"]["trial_state"] == "invalidated"


def test_existing_verifier_types_project_to_the_frozen_contract():
    spec = VerifierSpec("verify_example", 1, verifier_handler)
    passed = project_verification_result(spec, VerificationResult("passed", {"count": 1}))
    blocked = project_verification_result(
        spec,
        VerificationResult("blocked", {}, "verifier_unavailable"),
    )
    assert passed["trial_state"] == "succeeded"
    assert blocked["trial_state"] == "invalidated"
    assert blocked["verifier"]["contract_digest"] == spec.contract_digest

    with pytest.raises(ValueError, match="verifier_failure_code_missing"):
        project_verification_result(spec, VerificationResult("failed"))


def test_contract_fixture_is_synthetic_and_contains_no_secret_fields():
    serialized = FIXTURE.read_text(encoding="utf-8").lower()
    assert "password" not in serialized
    assert "access_token" not in serialized
    assert "api_key" not in serialized
    assert "expected_answer" not in serialized
