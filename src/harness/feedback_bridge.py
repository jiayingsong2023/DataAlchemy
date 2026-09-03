"""Project approved feedback into existing Task/Experience governance contracts."""

from __future__ import annotations

import json
from typing import Any

from core.evidence import EvidenceObjectStore, canonical_bytes, sha256
from harness.evaluation import EvaluationService
from harness.experience import (
    _put_immutable,
    publish_rag_task_bundle,
    validate_experience_bundle,
    validate_task_bundle,
)


def publish_feedback_task(
    store: EvidenceObjectStore,
    annotation: dict[str, Any],
    *,
    split: str,
    environment_snapshot: dict[str, Any],
    reset_contract: dict[str, Any],
    tool_contract: dict[str, Any],
    limits: dict[str, Any],
    retention_until: str,
) -> dict[str, Any]:
    """Publish a Task Bundle from one approved, evidence-bound correction."""
    label = annotation.get("label", {})
    if (
        annotation.get("status") != "approved"
        or annotation.get("training_allowed") is not True
        or split not in {"train", "validation"}
        or not isinstance(label.get("query"), str)
        or not label["query"].strip()
        or not isinstance(label.get("expected_response"), str)
        or not label["expected_response"].strip()
        or not label.get("expected_citations")
        or not annotation.get("source_acl_digest")
        or not annotation.get("training_permission_version")
    ):
        raise ValueError("feedback_task_source_invalid")
    citations = label.get("citations", [])
    expected = label["expected_citations"]
    expected_spans = {
        span_id for citation in expected for span_id in citation.get("source_span_ids", [])
    }
    sources = [
        citation
        for citation in citations
        if expected_spans & set(citation.get("source_span_ids", []))
    ]
    if not sources or len({item.get("source_sha256") for item in sources}) != 1:
        raise ValueError("feedback_task_source_ambiguous")
    source = sources[0]
    pages = [item.get("locator", {}).get("page") for item in sources]
    if any(type(page) is not int or page < 0 for page in pages):
        raise ValueError("feedback_task_locator_invalid")
    return publish_rag_task_bundle(
        store,
        {
            "case_id": f"feedback-{annotation['annotation_id']}",
            "query": label["query"].strip(),
            "split": split,
            "expected_status": "grounded",
            "expected_citation_count": len(expected),
            "required_pages": sorted(set(pages)),
            "required_substrings": [label["expected_response"].strip()],
            "source": {
                "source_uri": source["source_uri"],
                "sha256": source["source_sha256"],
                "pages": max(pages) + 1,
            },
        },
        tenant_id=annotation["tenant_id"],
        environment_snapshot=environment_snapshot,
        reset_contract=reset_contract,
        tool_contract=tool_contract,
        verifier_name="verify_rag_outcome",
        verifier_version=1,
        limits=limits,
        acl_sha256=annotation["source_acl_digest"],
        permission_version=annotation["training_permission_version"],
        retention_until=retention_until,
    )


def create_experience_review_candidate(
    store: EvidenceObjectStore,
    evaluations: EvaluationService,
    identity: dict[str, str],
    source_annotation: dict[str, Any],
    experience: dict[str, str],
) -> str:
    """Create a candidate annotation; a separate reviewer still grants training use."""
    body = store.get(experience["experience_ref"])
    if sha256(body) != experience["experience_sha256"]:
        raise ValueError("feedback_experience_hash_mismatch")
    bundle = validate_experience_bundle(json.loads(body))
    task_body = store.get(bundle["task_bundle_ref"])
    if sha256(task_body) != bundle["task_bundle_sha256"]:
        raise ValueError("feedback_task_hash_mismatch")
    task = validate_task_bundle(json.loads(task_body))
    label = source_annotation.get("label", {})
    if (
        source_annotation.get("status") != "approved"
        or source_annotation.get("training_allowed") is not True
        or bundle["tenant_id"] != source_annotation.get("tenant_id")
        or task["task"]["case_id"] != f"feedback-{source_annotation.get('annotation_id')}"
        or task["task"]["split"] not in {"train", "validation"}
        or bundle["labels"]["training_allowed"] is not False
    ):
        raise ValueError("feedback_experience_source_invalid")
    split_group = next(
        (
            item.get("source_version")
            for item in label.get("evidence_refs", [])
            if item.get("source_version")
        ),
        None,
    )
    if not split_group:
        raise ValueError("feedback_experience_split_group_missing")
    review = {
        "decision": "approved",
        "source_feedback_annotation_id": str(source_annotation["annotation_id"]),
        "experience_ref": experience["experience_ref"],
        "experience_sha256": experience["experience_sha256"],
        "task_bundle_id": bundle["task_bundle_id"],
        "run_id": bundle["run_id"],
        "trial_id": bundle["trial_id"],
        "split": task["task"]["split"],
        "split_group": split_group,
        "expected_response": label["expected_response"],
        "expected_citations": label["expected_citations"],
    }
    review_body = canonical_bytes(review)
    digest = sha256(review_body)
    key = f"tenants/{identity['tenant_id']}/annotations/experience/sha256/{digest}.json"
    _put_immutable(store, key, review_body)
    return evaluations.create_annotation(
        identity,
        run_id=bundle["run_id"],
        trial_id=bundle["trial_id"],
        kind="human_review",
        label=review,
        content_key=key,
        content_sha256=digest,
        source_acl_digest=source_annotation["source_acl_digest"],
    )
