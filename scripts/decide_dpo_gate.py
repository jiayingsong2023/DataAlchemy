"""Publish and independently verify the EL-4 DPO enablement decision."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.model_migration import (
    build_dpo_gate_decision,
    publish_dpo_gate_decision,
    validate_migration_report,
)
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--migration-report-ref", required=True)
    parser.add_argument("--migration-report-sha256", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("dpo_gate_database_url_missing")
    if not args.verifier_database_url or args.verifier_database_url == args.database_url:
        raise ValueError("dpo_gate_verifier_database_url_missing")

    identity = {"tenant_id": args.tenant_id, "username": "el4-verifier", "role": "admin"}
    services = ReadOnlyServices(args.verifier_database_url, identity)
    body = services.object_body(args.migration_report_ref)
    if body is None or sha256(body) != args.migration_report_sha256:
        raise ValueError("dpo_gate_migration_hash_mismatch")
    migration = validate_migration_report(json.loads(body))
    decision = build_dpo_gate_decision(
        tenant_id=args.tenant_id,
        migration_report=migration,
        migration_report_ref=args.migration_report_ref,
        migration_report_sha256=args.migration_report_sha256,
    )
    s3 = S3Utils()
    published = publish_dpo_gate_decision(S3EvidenceStore(s3.bucket, s3.client), decision)
    verified = (
        default_verifiers()
        .get("verify_dpo_gate", 1)
        .handler({"parameters": published}, identity, {}, services)
    )
    if verified.status != "passed":
        raise RuntimeError(f"dpo_gate_verification_failed:{verified.error_code}")
    print(json.dumps({**published, "decision": decision["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()
