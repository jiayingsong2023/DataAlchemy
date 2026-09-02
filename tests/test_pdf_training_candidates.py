import pytest

from scripts.build_pdf_training_candidates import build_candidates


def corpus():
    return {
        "tenant_id": "t1",
        "documents": [
            {
                "source_uri": "s3://bucket/raw/doc.pdf",
                "source_version": "sha256:source-v1",
                "acl_digest": "acl-v1",
                "trust_label": "untrusted_external",
                "spans": [
                    {
                        "span_id": "c1",
                        "text": "Support is open on Tuesday.",
                        "locator": {"page": 2, "paragraph": None},
                    },
                    {
                        "span_id": "c2",
                        "text": "P1 requires a ticket.",
                        "locator": {"page": 3, "paragraph": None},
                    },
                ],
            }
        ],
    }


def qa():
    base = {"review_status": "approved", "training_allowed": True, "permission_version": "p1"}
    return [
        {
            **base,
            "source_span_id": "c1",
            "split": "train",
            "instruction": "What is the support day?",
            "output": "Tuesday.",
        },
        {
            **base,
            "source_span_id": "c2",
            "split": "validation",
            "instruction": "What does P1 require?",
            "output": "A ticket.",
        },
    ]


def test_build_candidates_preserves_lineage_and_splits():
    rows, manifest = build_candidates(corpus(), qa())
    assert len(rows) == 2
    assert rows[0]["schema_version"] == "learning_candidate.v1"
    assert rows[0]["provenance"]["source_span_id"] == "c1"
    assert rows[0]["provenance"]["locator"]["page"] == 2
    assert rows[0]["split"] == "train"
    assert rows[0]["training_allowed"] is True
    assert manifest["train"] == 1
    assert manifest["validation"] == 1


def test_builder_rejects_unapproved_training_data():
    bad = qa()
    bad[0]["training_allowed"] = False
    with pytest.raises(ValueError, match="training_permission_missing"):
        build_candidates(corpus(), bad)
