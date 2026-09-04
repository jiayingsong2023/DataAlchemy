"""Approve one immutable Experience for an engineering SFT snapshot."""

from __future__ import annotations

import argparse
import json
import os

from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from harness.evaluation import EvaluationService
from harness.experience import _put_immutable, validate_experience_bundle, validate_task_bundle
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--experience-ref", required=True)
    parser.add_argument("--experience-sha256", required=True)
    parser.add_argument("--split-group", required=True)
    parser.add_argument("--expected-response", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url or not args.tenant_id.startswith("rtd-q3-"):
        raise ValueError("experience_review_environment_invalid")
    store = S3Utils()
    evidence = S3EvidenceStore(store.bucket, store.client)
    body = evidence.get(args.experience_ref)
    if sha256(body) != args.experience_sha256:
        raise ValueError("experience_review_hash_mismatch")
    bundle = validate_experience_bundle(json.loads(body))
    task = validate_task_bundle(json.loads(evidence.get(bundle["task_bundle_ref"])))
    if bundle["tenant_id"] != args.tenant_id:
        raise ValueError("experience_review_tenant_mismatch")
    label = {
        "decision": "approved",
        "experience_ref": args.experience_ref,
        "experience_sha256": args.experience_sha256,
        "task_bundle_id": bundle["task_bundle_id"],
        "run_id": bundle["run_id"],
        "trial_id": bundle["trial_id"],
        "split": task["task"]["split"],
        "split_group": args.split_group,
        "expected_response": args.expected_response,
        "expected_citations": [],
    }
    label_body = canonical_bytes(label)
    digest = sha256(label_body)
    key = f"tenants/{args.tenant_id}/annotations/experience/sha256/{digest}.json"
    _put_immutable(evidence, key, label_body)
    service = EvaluationService(args.database_url)
    creator = {"tenant_id": args.tenant_id, "username": "rtd-q3-curator", "role": "admin"}
    reviewer = {"tenant_id": args.tenant_id, "username": "rtd-q3-reviewer", "role": "reviewer"}
    annotation_id = service.create_annotation(
        creator,
        run_id=bundle["run_id"],
        trial_id=bundle["trial_id"],
        kind="human_review",
        label=label,
        content_key=key,
        content_sha256=digest,
        source_acl_digest=task["governance"]["acl_sha256"],
    )
    service.review_annotation(
        reviewer,
        annotation_id,
        status="approved",
        training_allowed=True,
        training_purpose="model_improvement",
        permission_version=task["governance"]["permission_version"],
        reason="RTD-Q3 isolated engineering qualification",
    )
    print(json.dumps({"annotation_id": annotation_id, "split": label["split"]}, sort_keys=True))


if __name__ == "__main__":
    main()
