import json
from unittest.mock import MagicMock

from src.core.evidence import ObjectNotFound, canonical_bytes, sha256
from src.harness.experience import publish_trial_experience
from src.harness.feedback_bridge import (
    create_experience_review_candidate,
    publish_feedback_task,
)


class Store(dict):
    def get(self, key):
        try:
            return self[key]
        except KeyError as error:
            raise ObjectNotFound(key) from error

    def put(self, key, body):
        self[key] = body


def annotation():
    citation = {
        "source_uri": "s3://bucket/source.pdf",
        "source_sha256": "1" * 64,
        "source_span_ids": ["span-1"],
        "locator": {"page": 0},
    }
    return {
        "annotation_id": "annotation-1",
        "tenant_id": "acme",
        "status": "approved",
        "training_allowed": True,
        "training_permission_version": "permission-v1",
        "source_acl_digest": "2" * 64,
        "label": {
            "query": "What is the code?",
            "expected_response": "The code is one.",
            "expected_citations": [
                {"source_span_ids": ["span-1"], "source_content_sha256": "3" * 64}
            ],
            "citations": [citation],
            "evidence_refs": [{"source_version": "sha256:" + "1" * 64}],
        },
    }


def test_reviewed_feedback_bridges_to_task_and_review_candidate():
    store = Store()
    source = annotation()
    assets = publish_feedback_task(
        store,
        source,
        split="train",
        environment_snapshot={"kind": "rag", "source_sha256": "1" * 64},
        reset_contract={"kind": "registered-script", "ref": "reset-v1", "sha256": "4" * 64},
        tool_contract={"name": "rag_chat", "version": 1, "contract_sha256": "5" * 64},
        limits={"max_steps": 1, "deadline_seconds": 300},
        retention_until="2027-09-02T00:00:00Z",
    )
    fingerprint = assets["fingerprint"]
    policy = {"do_sample": False}
    transcript = {
        "schema_version": "trial_transcript.v1",
        "trial_id": "trial-1",
        "case_id": "feedback-annotation-1",
        "task_bundle_id": fingerprint["task_bundle_id"],
        "environment_receipt_ref": "receipt.json",
        "environment_receipt_sha256": "6" * 64,
        "prompt": "What is the code?",
        "answer": "Wrong answer.",
        "status": "grounded",
        "citations": [],
        "latency_ms": 1.0,
        "model_fingerprint": {
            "schema_version": "model_fingerprint.v1",
            "model_id": "model-a",
            "model_sha256": "7" * 64,
            "tokenizer_sha256": "8" * 64,
            "chat_template_sha256": "9" * 64,
            "adapter_sha256": None,
        },
        "generation_policy": policy,
        "generation_policy_sha256": sha256(policy),
        "verifier": {
            "name": "verify_rag_outcome",
            "version": 1,
            "contract_digest": "a" * 64,
            "status": "failed",
            "error_code": "wrong_answer",
            "summary": {},
        },
    }
    store["manifest.json"] = canonical_bytes({"run_id": "run-1"})
    experience = publish_trial_experience(
        store,
        tenant_id="acme",
        trial={
            "trial_id": "trial-1",
            "run_id": "run-1",
            "state": "failed",
            "failure_code": "wrong_answer",
            "fingerprint": {**fingerprint, "environment_receipt_sha256": "6" * 64},
            "transcript_sha256": sha256(transcript),
        },
        transcript=transcript,
        source_manifest_ref="manifest.json",
        source_manifest_sha256=sha256(store["manifest.json"]),
    )
    evaluations = MagicMock()
    evaluations.create_annotation.return_value = "candidate-1"
    candidate = create_experience_review_candidate(
        store,
        evaluations,
        {"tenant_id": "acme", "username": "bridge", "role": "admin"},
        source,
        experience,
    )

    assert candidate == "candidate-1"
    label = evaluations.create_annotation.call_args.kwargs["label"]
    assert label["source_feedback_annotation_id"] == "annotation-1"
    assert label["split"] == "train"
    assert json.loads(store[evaluations.create_annotation.call_args.kwargs["content_key"]]) == label
