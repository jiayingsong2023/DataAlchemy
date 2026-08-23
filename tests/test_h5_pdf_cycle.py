import pytest

from scripts.rerollout_task_bundles import _rag_preflight
from scripts.run_h5_pdf_cycle import deterministic_job_key, run_job, training_dataset
from src.harness.job_runner import finish_evaluation_trials


def _annotation(index: int) -> dict:
    body = {"query": f"question-{index}", "answer": f"answer-{index}"}
    return {
        "annotation_id": f"annotation-{index}",
        "content_sha256": "a" * 64,
        "source_acl_digest": "acl",
        "training_purpose": "pdf_qa_improvement",
        "training_permission_version": "pdf-cycle-v1",
        "feedback": body,
    }


def test_pdf_training_dataset_requires_two_reviewed_examples():
    with pytest.raises(RuntimeError, match="two_approved"):
        training_dataset([_annotation(1)], "tenant", "purpose", "permission")


def test_pdf_training_dataset_has_train_and_validation_lineage():
    body, items = training_dataset(
        [_annotation(1), _annotation(2)], "tenant", "purpose", "permission"
    )
    assert len(body.splitlines()) == 2
    assert [item["split"] for item in items] == ["train", "validation"]
    assert all(item["tenant_id"] == "tenant" for item in items)


def test_h5_job_key_is_retry_stable_and_gate_scoped():
    first = deterministic_job_key("tenant", "run", "attempt", "lora", "a" * 64)
    retry = deterministic_job_key("tenant", "run", "attempt", "lora", "a" * 64)
    other_gate = deterministic_job_key("tenant", "run", "attempt", "evaluation", "a" * 64)
    assert first == retry
    assert first != other_gate


def test_h5_job_renews_attempt_lease_while_waiting():
    class Service:
        def request(self, *_args):
            return {"job_id": "job"}

        def reconcile(self, *_args):
            return type("Observation", (), {"state": "succeeded", "result": {"ok": True}})()

    beats = []
    result = run_job(
        Service(),
        {"task_id": "task"},
        {"tenant_id": "tenant"},
        kind="lora_train",
        root_run_id="run",
        attempt_id="attempt",
        gate_name="lora",
        input_key="input",
        input_sha256="a" * 64,
        heartbeat=lambda: beats.append(True),
    )

    assert result == {"ok": True}
    assert beats == [True]


def test_model_job_writes_transcript_before_finishing_trial():
    events = []

    class Store:
        def put_object(self, key, body, _content_type):
            events.append(("write", key, body))
            return True

    class Service:
        def finish_trial(self, _identity, trial_id, result, **refs):
            events.append(("finish", trial_id, result, refs))

    fingerprint = {
        "schema_version": "model_fingerprint.v1",
        "model_id": "model-a",
        "model_sha256": "a" * 64,
        "tokenizer_sha256": "b" * 64,
        "chat_template_sha256": "c" * 64,
        "adapter_sha256": None,
    }
    context = {
        "run_id": "run-1",
        "trial_ids": {"case-1": "trial-1"},
        "task_fingerprints": {"case-1": {"task_bundle_id": "sha256:" + "d" * 64}},
        "environment_receipts": {"case-1": {"ref": "receipt.json", "sha256": "e" * 64}},
    }
    result = {
        "output": {
            "cases": [
                {
                    "case_id": "case-1",
                    "prompt": "question",
                    "answer": "answer",
                    "status": "grounded",
                    "citations": [],
                    "latency_ms": 2.0,
                    "model_fingerprint": fingerprint,
                    "generation_policy_sha256": "f" * 64,
                    "verification": {
                        "name": "verify_rag_outcome",
                        "version": 1,
                        "status": "failed",
                        "error_code": "wrong_answer",
                        "summary": {},
                    },
                }
            ]
        }
    }
    finish_evaluation_trials(Service(), Store(), {"tenant_id": "acme"}, context, result)
    assert [event[0] for event in events] == ["write", "finish"]
    assert events[1][2]["state"] == "failed"


def test_rerollout_blocks_grounded_task_when_runtime_cannot_retrieve_fixture():
    asset = {
        "model_input": {"case_id": "case-1", "query": "question"},
        "verifier_input": {
            "criteria": {
                "expected_status": "grounded",
                "source": {"sha256": "a" * 64},
            }
        },
    }

    class Retriever:
        def query(self, *_args, **_kwargs):
            return []

    with pytest.raises(RuntimeError, match="rerollout_rag_fixture_unavailable"):
        _rag_preflight(Retriever(), [asset], {"tenant_id": "acme"})
