import json
from copy import deepcopy
from pathlib import Path

import pytest

from core.evidence import ObjectNotFound, canonical_bytes, sha256
from src.core.verifiers import VerificationResult, VerifierSpec, default_verifiers
from src.harness.experience import (
    finalize_environment_receipt,
    project_verification_result,
    publish_rag_task_bundle,
    publish_trial_experience,
    task_bundle_id,
    validate_environment_receipt,
    validate_experience_bundle,
    validate_experience_content,
    validate_task_bundle,
    validate_verifier_result,
)

FIXTURE = Path(__file__).parent / "fixtures" / "experience" / "tve_contract_cases.json"


def cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def verifier_handler(_criterion, _task, _result, _services):
    return VerificationResult("passed")


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


def published_task(store=None):
    store = store or MemoryStore()
    assets = publish_rag_task_bundle(
        store,
        {
            "case_id": "case-1",
            "query": "What is supported?",
            "input_sha256": "0" * 64,
            "expected_status": "abstained",
            "expected_answer": "No evidence.",
        },
        tenant_id="acme",
        environment_snapshot={"kind": "pdf-rag", "source_sha256": "1" * 64},
        reset_contract={"kind": "registered-script", "ref": "reset-v1", "sha256": "2" * 64},
        tool_contract={"name": "rag_chat", "version": 1, "contract_sha256": "3" * 64},
        verifier_name="verify_rag_outcome",
        verifier_version=1,
        limits={"max_steps": 8, "deadline_seconds": 300},
        acl_sha256="4" * 64,
        permission_version="task-use-v1",
        retention_until="2027-08-23T00:00:00Z",
    )
    return store, assets


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

    finalized = finalize_environment_receipt(
        payload["valid_environment_receipt"],
        {"mutations": []},
        cleanup_status="completed",
    )
    assert finalized["cleanup"]["status"] == "completed"
    assert len(finalized["final_state_delta_sha256"]) == 64

    cleanup_failed = finalize_environment_receipt(
        payload["valid_environment_receipt"],
        {"mutations": ["temporary-key"]},
        cleanup_status="failed",
        cleanup_error="environment_cleanup_failed",
    )
    assert cleanup_failed["state"] == "invalidated"


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

    with pytest.raises(ValueError, match="contract_secret_field_forbidden"):
        validate_experience_content({"api_key": "not-a-real-key"}, "acme")


def test_rag_task_bundle_publishes_model_input_separately_from_hidden_criteria():
    store, assets = published_task()
    fingerprint = assets["fingerprint"]
    bundle = json.loads(store.get(fingerprint["task_bundle_ref"]))
    model_input = json.loads(store.get(fingerprint["task_input_ref"]))
    verifier_input = json.loads(store.get(fingerprint["verifier_input_ref"]))

    assert task_bundle_id(bundle) == fingerprint["task_bundle_id"]
    assert set(model_input) == {"case_id", "query"}
    assert "expected_answer" not in json.dumps(bundle)
    assert "expected_answer" not in model_input
    assert verifier_input["criteria"]["expected_answer"] == "No evidence."

    _, repeated = published_task(store)
    assert repeated["fingerprint"] == fingerprint

    store.objects[fingerprint["task_bundle_ref"]] = b"tampered"
    with pytest.raises(RuntimeError, match="task_asset_key_conflict"):
        published_task(store)


def test_verify_task_bundle_checks_published_assets_and_tenant():
    store, assets = published_task()

    class Services:
        def object_body(self, key):
            return store.objects.get(key)

    verifier = default_verifiers().get("verify_task_bundle", 1)
    result = {"output": assets["fingerprint"]}
    passed = verifier.handler({}, {"tenant_id": "acme"}, result, Services())
    wrong_tenant = verifier.handler({}, {"tenant_id": "other"}, result, Services())

    assert passed.status == "passed"
    assert wrong_tenant.error_code == "task_bundle_tenant_mismatch"


def test_valid_trial_publishes_content_addressed_non_trainable_experience():
    store, assets = published_task()
    fingerprint = assets["fingerprint"]
    policy = {"max_new_tokens": 16, "do_sample": False}
    transcript = {
        "schema_version": "trial_transcript.v1",
        "trial_id": "trial-1",
        "case_id": "case-1",
        "task_bundle_id": fingerprint["task_bundle_id"],
        "environment_receipt_ref": "receipt.json",
        "environment_receipt_sha256": "5" * 64,
        "prompt": "Use evidence and answer.",
        "answer": "No evidence.",
        "status": "abstained",
        "citations": [],
        "latency_ms": 2.5,
        "model_fingerprint": {
            "schema_version": "model_fingerprint.v1",
            "model_id": "model-a",
            "model_sha256": "6" * 64,
            "tokenizer_sha256": "7" * 64,
            "chat_template_sha256": "8" * 64,
            "adapter_sha256": None,
        },
        "generation_policy": policy,
        "generation_policy_sha256": sha256(policy),
        "verifier": {
            "name": "verify_rag_outcome",
            "version": 1,
            "contract_digest": "9" * 64,
            "status": "failed",
            "error_code": "wrong_answer",
            "summary": {},
        },
    }
    transcript_body = canonical_bytes(transcript)
    manifest = {
        "schema_version": 1,
        "run": {"run_id": "run-1", "tenant_id": "acme"},
    }
    store.objects["manifest.json"] = canonical_bytes(manifest)
    trial = {
        "trial_id": "trial-1",
        "run_id": "run-1",
        "state": "failed",
        "failure_code": "wrong_answer",
        "fingerprint": {**fingerprint, "environment_receipt_sha256": "5" * 64},
        "transcript_sha256": sha256(transcript_body),
    }

    published = publish_trial_experience(
        store,
        tenant_id="acme",
        trial=trial,
        transcript=transcript,
        source_manifest_ref="manifest.json",
        source_manifest_sha256=sha256(store.objects["manifest.json"]),
    )
    bundle = json.loads(store.get(published["experience_ref"]))

    assert validate_experience_bundle(bundle) == bundle
    assert bundle["labels"] == {
        "success": False,
        "failure_code": "wrong_answer",
        "training_allowed": False,
        "annotation_refs": [],
    }
    assert [event["sequence"] for event in bundle["events"]] == list(
        range(1, len(bundle["events"]) + 1)
    )
    model_call = json.loads(
        store.get(
            next(event for event in bundle["events"] if event["type"] == "model_call")[
                "content_ref"
            ]
        )
    )
    assert model_call["usage"]["unavailable_reason"] == "provider_usage_not_exposed"

    broken = deepcopy(bundle)
    broken["events"][1]["sequence"] = 9
    with pytest.raises(ValueError, match="experience_event_sequence_invalid"):
        validate_experience_bundle(broken)

    retried = deepcopy(bundle)
    model_index = next(
        index for index, event in enumerate(retried["events"]) if event["type"] == "model_call"
    )
    retry = {
        **deepcopy(retried["events"][model_index]),
        "call_id": "call-retry",
        "retry_of": retried["events"][model_index]["call_id"],
    }
    retried["events"].insert(model_index + 1, retry)
    for sequence, event in enumerate(retried["events"], 1):
        event["sequence"] = sequence
    assert validate_experience_bundle(retried)["events"][model_index + 1]["retry_of"]
    retried["events"][model_index + 1]["retry_of"] = "unknown-call"
    with pytest.raises(ValueError, match="experience_retry_lineage_invalid"):
        validate_experience_bundle(retried)

    with pytest.raises(ValueError, match="experience_outcome_not_publishable"):
        publish_trial_experience(
            store,
            tenant_id="acme",
            trial={**trial, "state": "invalidated"},
            transcript=transcript,
            source_manifest_ref="manifest.json",
            source_manifest_sha256=sha256(store.objects["manifest.json"]),
        )
