"""Publish and independently verify the EL-5 RL/Agent Lightning decision."""

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
    build_rl_gate_decision,
    publish_rl_gate_decision,
    validate_dpo_gate_decision,
)
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--dpo-gate-decision-ref", required=True)
    parser.add_argument("--dpo-gate-decision-sha256", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("rl_gate_database_url_missing")
    if not args.verifier_database_url or args.verifier_database_url == args.database_url:
        raise ValueError("rl_gate_verifier_database_url_missing")

    identity = {"tenant_id": args.tenant_id, "username": "el5-verifier", "role": "admin"}
    services = ReadOnlyServices(args.verifier_database_url, identity)
    body = services.object_body(args.dpo_gate_decision_ref)
    if body is None or sha256(body) != args.dpo_gate_decision_sha256:
        raise ValueError("rl_gate_dpo_hash_mismatch")
    dpo = validate_dpo_gate_decision(json.loads(body))
    decision = build_rl_gate_decision(
        tenant_id=args.tenant_id,
        dpo_gate_decision=dpo,
        dpo_gate_decision_ref=args.dpo_gate_decision_ref,
        dpo_gate_decision_sha256=args.dpo_gate_decision_sha256,
    )
    s3 = S3Utils()
    published = publish_rl_gate_decision(S3EvidenceStore(s3.bucket, s3.client), decision)
    verified = (
        default_verifiers()
        .get("verify_rl_gate", 1)
        .handler({"parameters": published}, identity, {}, services)
    )
    if verified.status != "passed":
        raise RuntimeError(f"rl_gate_verification_failed:{verified.error_code}")
    print(
        json.dumps(
            {
                **published,
                "decision": decision["decision"],
                "agent_lightning": decision["agent_lightning"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
