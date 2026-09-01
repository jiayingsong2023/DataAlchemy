"""Publish valid TVE trial transcripts as governed Experience Bundles."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent_runtime import AgentRuntime
from core.evidence import S3EvidenceStore
from core.tool_contracts import ToolRegistry
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.evaluation import validate_trial_transcript
from harness.experience import publish_trial_experience
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-id", action="append", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--username", default="el1-publisher")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("experience_database_url_missing")

    identity = {"tenant_id": args.tenant_id, "username": args.username, "role": "admin"}
    service = ReadOnlyServices(args.database_url, identity)
    s3 = S3Utils()
    store = S3EvidenceStore(s3.bucket, s3.client)
    runtime = AgentRuntime(args.database_url, ToolRegistry())
    published = []
    for trial_id in args.trial_id:
        trial = service.trial(trial_id)
        if trial is None:
            raise ValueError(f"experience_trial_missing:{trial_id}")
        transcript_body = service.object_body(trial.get("transcript_key"))
        if transcript_body is None:
            raise ValueError(f"experience_transcript_missing:{trial_id}")
        transcript = validate_trial_transcript(json.loads(transcript_body))
        manifest = service.run_manifest(str(trial["run_id"]))
        if manifest is None or manifest["state"] != "published":
            raise ValueError(f"experience_manifest_unpublished:{trial_id}")
        descriptor = publish_trial_experience(
            store,
            tenant_id=args.tenant_id,
            trial=trial,
            transcript=transcript,
            source_manifest_ref=manifest["object_key"],
            source_manifest_sha256=manifest["manifest_sha256"],
        )
        result = (
            default_verifiers()
            .get("verify_experience_bundle", 1)
            .handler(
                {"parameters": descriptor},
                {"tenant_id": args.tenant_id},
                {"output": {}},
                service,
            )
        )
        if result.status != "passed":
            raise RuntimeError(f"experience_verification_failed:{result.error_code}")
        runtime.record_event(
            str(trial["task_id"]),
            identity,
            "experience_published",
            {"run_id": str(trial["run_id"]), **descriptor},
        )
        published.append({"trial_id": trial_id, **descriptor, **result.summary})
    print(json.dumps({"published": published}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
