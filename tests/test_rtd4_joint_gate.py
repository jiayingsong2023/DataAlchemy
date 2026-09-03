from src.harness.rtd4_joint_gate import _artifact_evidence, _score


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
