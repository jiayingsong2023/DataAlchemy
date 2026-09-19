import pytest

from src.harness.rtd4_joint_gate import (
    _artifact_evidence,
    _evaluation_release_passed,
    _percentile,
    _score,
)


@pytest.mark.asyncio
async def test_local_arm_keeps_deployment_binding_separate_from_answering(monkeypatch):
    from types import SimpleNamespace

    from src.harness import rtd4_joint_gate as joint
    from src.rag.answering import GroundedAnswering

    operations = []

    class Runtime:
        def __init__(self, **_kwargs):
            self.model_manager = SimpleNamespace(unload_models=lambda: operations.append("unload"))

        def check_and_reload_adapter(self, **_kwargs):
            operations.append("load")

        def model_status(self, _identity):
            operations.append("status")
            return {"adapter_id": "adapter-1", "loaded": True}

    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = None
    monkeypatch.setattr(joint, "AdapterRuntime", Runtime)
    monkeypatch.setattr(joint, "GroundedAnswering", lambda: answering)
    # _run_arm owns these environment mutations; restore them when the test exits.
    monkeypatch.setenv("H5_LORA_MODE", "disabled")
    monkeypatch.setenv("MODEL_RELEASE_TENANT_ID", "")
    result = await joint._run_arm(
        "adapter_rag",
        {"tenant_id": "test"},
        [
            {
                "case_id": f"empty-{index}",
                "query": "question",
                "required_substrings": [],
                "required_pages": [],
            }
            for index in range(2)
        ],
        {f"empty-{index}": [] for index in range(2)},
    )
    assert operations == ["load", "status", "unload"]
    assert result["deployment_model_status"]["adapter_id"] == "adapter-1"
    assert result["model_execution"] == {"tenant_id": "test", "generation": "not_used"}
    assert result["generation_used"] is False
    assert result["cases"][0]["model_response_sha256"] is None


def test_percentile_uses_inclusive_observed_latency():
    assert _percentile([1, 2, 3, 4, 5], 95) == 4.8


def test_joint_gate_requires_grounded_text_page_and_lineage():
    case = {"required_substrings": ["answer"], "required_pages": [2]}
    citation = {
        "locator": {"page": 2},
        "source_span_ids": ["span-1"],
        "source_content_sha256": "a" * 64,
        "acl_digest": "b" * 64,
    }

    assert all(
        value
        for key, value in _score(case, "the answer", [citation]).items()
        if key.endswith("_passed")
    )
    assert not _score(case, "wrong", [citation])["required_text_passed"]


def test_artifact_evidence_matches_adapter_runtime_digest(monkeypatch):
    class FakeS3:
        def __init__(self, bucket):
            self.bucket = bucket

        def list_objects(self, prefix):
            return [{"Key": f"{prefix}/b"}, {"Key": f"{prefix}/a"}]

        def get_object_body(self, key):
            return key.rsplit("/", 1)[-1].encode()

    monkeypatch.setattr("src.harness.rtd4_joint_gate.S3Utils", FakeS3)

    assert _artifact_evidence("bucket", "adapter") == {
        "bucket": "bucket",
        "prefix": "adapter",
        "sha256": "486b34250bd4400c0aa90516fce9a9c0633a922eb40d0828cf299bc4e825acf4",
        "size": 2,
    }


def test_evaluation_release_evidence_binds_same_adapter():
    class Services:
        def evaluation(self, _evaluation_id):
            return {"state": "passed", "subject_type": "adapter", "subject_ref": "adapter-1"}

        def release(self, _release_id):
            return {
                "status": "promoted",
                "adapter_id": "adapter-1",
                "evaluation_id": "evaluation-1",
            }

        def adapter(self, adapter_id):
            return {"state": "verified"} if adapter_id == "adapter-1" else None

    assert _evaluation_release_passed(Services(), "evaluation-1", "release-1", "adapter-1")
    assert not _evaluation_release_passed(Services(), "evaluation-1", "release-1", "adapter-2")
