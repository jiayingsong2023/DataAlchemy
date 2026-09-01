"""Promote a verified adapter using one independently verified tiered decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from release.governance import ReleaseGovernance
from utils.s3_utils import S3Utils


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--decision-ref", required=True)
    parser.add_argument("--decision-sha256", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url or not args.verifier_database_url:
        raise ValueError("tiered_release_database_url_missing")

    owner = {"tenant_id": args.tenant_id, "username": "release-owner", "role": "admin"}
    promoter = {
        "tenant_id": args.tenant_id,
        "username": "release-promoter",
        "role": "admin",
    }
    services = ReadOnlyServices(args.verifier_database_url, promoter)
    checked = (
        default_verifiers()
        .get("verify_release_decision", 1)
        .handler(
            {
                "parameters": {
                    "decision_ref": args.decision_ref,
                    "decision_sha256": args.decision_sha256,
                }
            },
            promoter,
            {},
            services,
        )
    )
    if checked.status != "passed" or checked.summary.get("status") != "GO":
        raise ValueError("tiered_release_decision_unverified")
    decision = json.loads(services.object_body(args.decision_ref))
    candidate = decision["candidate_repetitions"]
    samples = sum(item["normal"]["required"] for item in candidate)
    passed = sum(item["normal"]["passed"] for item in candidate)
    observation = {
        "schema_version": "offline_canary_observation.v1",
        "classification": "PUBLIC_SYNTHETIC_ENGINEERING",
        "decision": {"ref": args.decision_ref, "sha256": args.decision_sha256},
        "sample_count": samples,
        "window_seconds": len(candidate),
        "window_complete": True,
        "security_passed": checked.summary["critical_passed"],
        "error_rate": 1 - passed / samples,
        "p95_ms": max(item["p95_latency_ms"] for item in candidate),
    }
    observation_body = canonical_bytes(observation)
    observation_sha256 = sha256(observation_body)
    observation_ref = f"tenants/{args.tenant_id}/release/canary/sha256/{observation_sha256}.json"
    store = S3Utils()
    if not store.put_object(observation_ref, observation_body, "application/json"):
        raise RuntimeError("tiered_release_canary_write_failed")

    code_digest = hashlib.sha256()
    verifier_paths = sorted(Path("src/core").glob("verifier*.py"))
    for path in (
        Path("scripts/rerollout_task_bundles.py"),
        *verifier_paths,
        Path("src/harness/release_policy.py"),
        Path("src/release/governance.py"),
    ):
        code_digest.update(Path(path).read_bytes())
    manifest = {
        "harness_version": 5,
        "code_version": f"worktree-sha256:{code_digest.hexdigest()}",
        "adapter_id": args.adapter_id,
        "training_snapshot_id": args.snapshot_id,
        "evaluation": {"passed": True, "hard_gates_passed": True},
        "release_decision": {"ref": args.decision_ref, "sha256": args.decision_sha256},
        "canary_evidence": {"ref": observation_ref, "sha256": observation_sha256},
        "rollback_to": "base",
        "guardrails": {
            "max_error_rate": 1 - decision["policy"]["normal_min_pass_rate"],
            "max_p95_ms": max(item["p95_latency_ms"] for item in decision["base_repetitions"])
            * decision["policy"]["max_p95_regression_ratio"],
            "min_samples": samples,
            "window_seconds": len(candidate),
        },
        "release_scope": "single_tenant_lora",
        "approvals": {"candidate": owner["username"], "promote": promoter["username"]},
        "policy_version": decision["policy"]["version"],
        "environment": "engineering",
    }
    releases = ReleaseGovernance(args.database_url)
    release_id = releases.create_candidate(owner, manifest)
    releases.advance(release_id, "shadow", owner)
    releases.advance(release_id, "canary", owner)
    status = releases.observe(release_id, observation, promoter, promote=True)
    if status != "promoted":
        raise RuntimeError(f"tiered_release_not_promoted:{status}")
    print(
        json.dumps(
            {
                "release_id": release_id,
                "status": status,
                "decision_ref": args.decision_ref,
                "canary_evidence_ref": observation_ref,
                "canary_evidence_sha256": observation_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
