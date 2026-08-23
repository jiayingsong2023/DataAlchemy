import pytest

from src.core.verifiers import VerificationResult
from src.harness.evaluation import (
    EvaluationService,
    build_gap_report,
    model_fingerprint_digest,
    validate_evaluation_pair,
    validate_model_fingerprint,
    validate_suite_manifest,
    validate_training_items,
    validate_trial_result,
    validate_trial_transcript,
)
from src.harness.evaluation_runner import run_evaluation
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


def model_fingerprint(model_id="model-a"):
    return {
        "schema_version": "model_fingerprint.v1",
        "model_id": model_id,
        "model_sha256": ("a" if model_id == "model-a" else "b") * 64,
        "tokenizer_sha256": "c" * 64,
        "chat_template_sha256": "d" * 64,
        "adapter_sha256": None,
    }


def test_suite_manifest_is_fixed_and_unique():
    result = validate_suite_manifest(suite())
    assert result["version"] == "h5-suite-1"
    with pytest.raises(ValueError, match="suite_case_duplicate"):
        validate_suite_manifest({**suite(), "cases": suite()["cases"] * 2})


def test_suite_manifest_derives_query_hash_and_preserves_source_fixture():
    result = validate_suite_manifest(
        {
            "version": "pdf-v1",
            "source": {"path": "fixture.pdf", "sha256": "b" * 64},
            "cases": [{"case_id": "case-1", "query": "question"}],
        }
    )
    assert len(result["cases"][0]["input_sha256"]) == 64
    assert result["source"]["sha256"] == "b" * 64


def test_invalidated_trial_requires_reason_and_training_has_two_splits():
    with pytest.raises(ValueError, match="invalidated_reason_missing"):
        validate_trial_result({"state": "invalidated"})
    assert (
        validate_trial_result({"state": "invalidated", "invalid_reason": "fixture_missing"})
        == "invalidated"
    )
    with pytest.raises(ValueError, match="snapshot_validation_split_missing"):
        validate_training_items([item("one")])
    assert len(validate_training_items([item("one"), item("two", "validation")])) == 2


def test_trial_transcript_and_gap_report_keep_invalid_out_of_capability_denominator():
    first = validate_model_fingerprint(model_fingerprint())
    second = validate_model_fingerprint(model_fingerprint("model-b"))
    transcript = {
        "schema_version": "trial_transcript.v1",
        "trial_id": "trial-1",
        "case_id": "case-1",
        "task_bundle_id": "sha256:" + "e" * 64,
        "environment_receipt_ref": "receipt.json",
        "environment_receipt_sha256": "f" * 64,
        "prompt": "question",
        "answer": "answer",
        "status": "grounded",
        "citations": [],
        "latency_ms": 1.5,
        "model_fingerprint": first,
        "generation_policy_sha256": "1" * 64,
        "verifier": {
            "name": "verify_rag_outcome",
            "version": 1,
            "contract_digest": "2" * 64,
            "status": "failed",
        },
    }
    assert validate_trial_transcript(transcript)["trial_id"] == "trial-1"
    outcomes = [
        {
            "task_bundle_id": "sha256:" + bundle * 64,
            "case_id": f"case-{bundle}",
            "target_fingerprint_sha256": model_fingerprint_digest(target),
            "state": state,
        }
        for bundle, states in (("3", ("succeeded", "failed")), ("4", ("invalidated", "failed")))
        for target, state in zip((first, second), states, strict=True)
    ]
    report = build_gap_report(
        [first, second],
        outcomes,
        generation_policy_sha256="1" * 64,
        verifier_contract_digest="2" * 64,
    )
    assert [item["classification"] for item in report["tasks"]] == ["weak", "invalid"]
    assert report["metrics"] == {
        "valid_tasks": 1,
        "invalid_tasks": 1,
        "capability_denominator": 1,
    }


def test_valid_trial_cannot_finish_before_model_transcript_exists():
    service = EvaluationService("postgresql://unused")
    with pytest.raises(ValueError, match="valid_trial_transcript_missing"):
        service.finish_trial(
            {"tenant_id": "acme"},
            "trial-1",
            {"state": "succeeded", "model_fingerprint": model_fingerprint()},
        )


def test_training_items_reject_permission_and_duplicates():
    with pytest.raises(ValueError, match="training_permission_missing"):
        validate_training_items(
            [{**item("one"), "training_allowed": False}, item("two", "validation")]
        )
    with pytest.raises(ValueError, match="snapshot_source_duplicate"):
        validate_training_items(
            [item("one", source_id="same"), item("two", "validation", source_id="same")]
        )


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
                "verifier_cases": [],
            }
        )
    assert (
        validate_evaluation_context(
            {
                "harness_version": 5,
                "run_id": "run-1",
                "tenant_id": "acme",
                "username": "tester",
                "role": "admin",
                "evaluation_id": "evaluation-1",
                "suite_sha256": "d" * 64,
                "database_url": "postgresql://example",
                "cases": [{"case_id": "case-1", "query": "question"}],
                "verifier_cases": [
                    {
                        "schema_version": "rag_verifier_input.v1",
                        "case_id": "case-1",
                        "criteria": {"required_substrings": []},
                    }
                ],
            }
        )["evaluation_id"]
        == "evaluation-1"
    )

    leaked = {
        "harness_version": 5,
        "run_id": "run-1",
        "tenant_id": "acme",
        "username": "tester",
        "role": "admin",
        "evaluation_id": "evaluation-1",
        "suite_sha256": "d" * 64,
        "database_url": "postgresql://example",
        "cases": [{"case_id": "case-1", "query": "question", "expected_answer": "hidden"}],
        "verifier_cases": [
            {
                "schema_version": "rag_verifier_input.v1",
                "case_id": "case-1",
                "criteria": {"expected_answer": "hidden"},
            }
        ],
    }
    with pytest.raises(ValueError, match="h5_evaluation_model_input_not_sanitized"):
        validate_evaluation_context(leaked)


def test_trial_registration_requires_task_bundle_fingerprint_before_database_access():
    service = EvaluationService("postgresql://unused")
    with pytest.raises(ValueError, match="trial_task_bundle_fingerprint_missing"):
        service.register_trial(
            {"tenant_id": "acme"},
            "evaluation-id",
            {"tenant_id": "acme", "run_id": "run-id", "task_id": "task-id"},
            case_id="case-1",
            trial_no=1,
            fingerprint={},
        )


def test_evaluator_keeps_verifier_criteria_out_of_model_input():
    observed = []
    context = {
        "harness_version": 5,
        "run_id": "run-1",
        "tenant_id": "acme",
        "username": "tester",
        "role": "admin",
        "evaluation_id": "evaluation-1",
        "suite_sha256": "d" * 64,
        "database_url": "postgresql://example",
        "cases": [{"case_id": "case-1", "query": "question"}],
        "verifier_cases": [
            {
                "schema_version": "rag_verifier_input.v1",
                "case_id": "case-1",
                "criteria": {"required_substrings": ["supported"]},
            }
        ],
        "predict": lambda query: observed.append(query) or "supported answer",
    }
    result = run_evaluation(context)
    assert observed == ["question"]
    assert result["hard_gates"]["passed"] is False
    case = result["output"]["cases"][0]
    assert case["answer"] == "supported answer"
    assert case["assertions"] == [
        {
            "name": "required_substring",
            "kind": "configuration_smoke",
            "value": "supported",
            "passed": True,
        }
    ]
    assert case["verification"]["status"] == "blocked"
    assert case["prompt"].startswith("### Instruction:")
    assert case["latency_ms"] >= 0

    structured = {
        **context,
        "predict": lambda _query: {
            "answer": "supported answer",
            "status": "grounded",
            "citations": [{"chunk_id": "chunk-1"}],
            "evidence_refs": [{"ref": "answer.json", "sha256": "a" * 64}],
        },
        "verify": lambda _criteria, _output: VerificationResult(
            "passed", {"hard_gates": {"passed": True}}
        ),
    }
    verified = run_evaluation(structured)
    assert verified["hard_gates"]["passed"] is True
    assert verified["output"]["cases"][0]["evidence_refs"][0]["ref"] == "answer.json"
