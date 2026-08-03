import pytest

from src.harness.evaluation import (
    validate_evaluation_pair,
    validate_suite_manifest,
    validate_training_items,
    validate_trial_result,
)
from src.harness.jobs import validate_evaluation_context, validate_training_context


def suite():
    return {
        "version": "h5-suite-1",
        "policy_version": "h5-policy-1",
        "cases": [{"case_id": "case-1", "input_sha256": "a" * 64}],
    }


def item(item_id, split="train", source_id=None):
    return {
        "item_id": item_id,
        "split": split,
        "source_type": "trajectory_annotation",
        "source_id": source_id or item_id,
        "source_sha256": "b" * 64,
        "training_allowed": True,
        "training_purpose": "deployment_model_improvement",
        "training_permission_version": "permission-1",
    }


def test_suite_manifest_is_fixed_and_unique():
    result = validate_suite_manifest(suite())
    assert result["version"] == "h5-suite-1"
    with pytest.raises(ValueError, match="suite_case_duplicate"):
        validate_suite_manifest({**suite(), "cases": suite()["cases"] * 2})


def test_invalidated_trial_requires_reason_and_training_has_two_splits():
    with pytest.raises(ValueError, match="invalidated_reason_missing"):
        validate_trial_result({"state": "invalidated"})
    assert validate_trial_result({"state": "invalidated", "invalid_reason": "fixture_missing"}) == "invalidated"
    with pytest.raises(ValueError, match="snapshot_validation_split_missing"):
        validate_training_items([item("one")])
    assert len(validate_training_items([item("one"), item("two", "validation")])) == 2


def test_training_items_reject_permission_and_duplicates():
    with pytest.raises(ValueError, match="training_permission_missing"):
        validate_training_items([{**item("one"), "training_allowed": False}, item("two", "validation")])
    with pytest.raises(ValueError, match="snapshot_source_duplicate"):
        validate_training_items([item("one", source_id="same"), item("two", "validation", source_id="same")])


def test_evaluation_pair_requires_same_suite_and_hard_gates():
    base = {
        "suite_sha256": "a" * 64,
        "policy_version": "h5-policy-1",
        "required_trials": 3,
        "state": "passed",
    }
    candidate = {
        **base,
        "state": "passed",
        "hard_gates": {"passed": True},
        "invalidated_trials": 0,
    }
    validate_evaluation_pair(base, candidate)
    with pytest.raises(ValueError, match="evaluation_suite_sha256_mismatch"):
        validate_evaluation_pair(base, {**candidate, "suite_sha256": "c" * 64})


def test_worker_contexts_fail_closed_before_external_jobs():
    training = {
        "harness_version": 5,
        "run_id": "run-1",
        "tenant_id": "acme",
        "username": "trainer",
        "role": "admin",
        "snapshot_id": "snapshot-1",
        "snapshot_state": "approved",
        "dataset_key": "runs/run-1/dataset.jsonl",
        "dataset_sha256": "a" * 64,
        "base_model_digest": "b" * 64,
        "tokenizer_digest": "c" * 64,
        "model_id": "/app/data/models/TinyLlama",
        "database_url": "postgresql://example",
        "base_evaluation_id": "evaluation-1",
        "base_evaluation_passed": True,
        "output_prefix": "adapters/run-1",
    }
    assert validate_training_context(training)["snapshot_state"] == "approved"
    with pytest.raises(ValueError, match="h5_training_prerequisite_failed"):
        validate_training_context({**training, "snapshot_state": "candidate"})
    with pytest.raises(ValueError, match="h5_evaluation_worker_role_invalid"):
        validate_evaluation_context(
            {
                "harness_version": 5,
                "run_id": "run-1",
                "tenant_id": "acme",
                "username": "tester",
                "role": "service",
                "evaluation_id": "evaluation-1",
                "suite_sha256": "d" * 64,
                "database_url": "postgresql://example",
                "cases": [{"case_id": "case-1"}],
            }
        )
    assert validate_evaluation_context(
        {
            "harness_version": 5,
            "run_id": "run-1",
            "tenant_id": "acme",
            "username": "tester",
            "role": "admin",
            "evaluation_id": "evaluation-1",
            "suite_sha256": "d" * 64,
            "database_url": "postgresql://example",
            "cases": [{"case_id": "case-1"}],
        }
    )["evaluation_id"] == "evaluation-1"
