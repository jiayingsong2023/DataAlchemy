from src.harness.rtd4_joint_gate import _score


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
