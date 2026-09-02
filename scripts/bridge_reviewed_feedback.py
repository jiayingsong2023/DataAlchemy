"""Publish a reviewed-feedback Task or its completed trial Experience."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore
from core.verifiers import ReadOnlyServices
from harness.evaluation import EvaluationService, validate_trial_transcript
from harness.experience import publish_trial_experience
from harness.feedback_bridge import create_experience_review_candidate, publish_feedback_task
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("task", "experience"))
    parser.add_argument("--annotation-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--username", default="feedback-bridge")
    parser.add_argument("--context", type=Path)
    parser.add_argument("--split", choices=("train", "validation"))
    parser.add_argument("--trial-id")
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("feedback_bridge_database_url_missing")
    identity = {"tenant_id": args.tenant_id, "username": args.username, "role": "admin"}
    services = ReadOnlyServices(args.database_url, identity)
    annotation = services.annotation(args.annotation_id)
    if annotation is None:
        raise ValueError("feedback_bridge_annotation_missing")
    s3 = S3Utils()
    store = S3EvidenceStore(s3.bucket, s3.client)
    if args.mode == "task":
        if not args.context or not args.split:
            raise ValueError("feedback_bridge_task_arguments_missing")
        context = json.loads(args.context.read_text(encoding="utf-8"))
        result = publish_feedback_task(store, annotation, split=args.split, **context)
    else:
        if not args.trial_id:
            raise ValueError("feedback_bridge_trial_missing")
        trial = services.trial(args.trial_id)
        if trial is None:
            raise ValueError("feedback_bridge_trial_missing")
        transcript_body = services.object_body(trial.get("transcript_key"))
        manifest = services.run_manifest(str(trial["run_id"]))
        if transcript_body is None or manifest is None or manifest["state"] != "published":
            raise ValueError("feedback_bridge_trial_unpublished")
        experience = publish_trial_experience(
            store,
            tenant_id=args.tenant_id,
            trial=trial,
            transcript=validate_trial_transcript(json.loads(transcript_body)),
            source_manifest_ref=manifest["object_key"],
            source_manifest_sha256=manifest["manifest_sha256"],
        )
        candidate = create_experience_review_candidate(
            store,
            EvaluationService(args.database_url),
            identity,
            annotation,
            experience,
        )
        result = {**experience, "candidate_annotation_id": candidate}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
