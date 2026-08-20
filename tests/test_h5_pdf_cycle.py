import pytest

from scripts.run_h5_pdf_cycle import deterministic_job_key, run_job, training_dataset


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
