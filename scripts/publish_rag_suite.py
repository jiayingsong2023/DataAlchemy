"""Publish one frozen RAG suite and reset its registered test environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore
from harness.evaluation import validate_suite_manifest
from harness.experience import publish_environment_receipt, publish_rag_task_bundle
from scripts.reset_pilot_environment import (
    build_environment_receipt,
    execute_reset,
    load_environment,
    preflight_environment,
    reset_plan,
    runtime_image_digest,
)
from scripts.run_h5_pdf_cycle import build_runtime
from utils.s3_utils import S3Utils


def digest(body: bytes | dict) -> str:
    if isinstance(body, dict):
        body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument(
        "--registry", type=Path, default=Path("deploy/pilot-environments.example.yaml")
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if not args.execute or not args.database_url:
        raise SystemExit("execute_and_database_url_required")

    environment = load_environment(args.registry, args.environment)
    plan = reset_plan(environment)
    expected = f"reset:{args.environment}:{plan['plan_sha256'][:12]}"
    if args.confirm != expected:
        raise SystemExit(f"confirmation_required:{expected}")
    suite = validate_suite_manifest(json.loads(args.suite.read_text(encoding="utf-8")))
    if suite.get("source", {}).get("sha256") != environment["fixture_sha256"]:
        raise ValueError("suite_environment_fixture_mismatch")

    s3 = S3Utils()
    store = S3EvidenceStore(s3.bucket, s3.client)
    tool = build_runtime(args.database_url, s3).tools.get("h5_trial_capture")
    tool_contract = {
        "name": tool.name,
        "version": tool.version,
        "contract_sha256": tool.contract_digest,
    }
    snapshot = {
        "schema_version": "tve_pdf_environment.v1",
        "tenant_id": environment["tenant_id"],
        "environment_id": environment["environment_id"],
        "fixture_sha256": environment["fixture_sha256"],
        "source_permission_active": True,
    }
    reset_script = Path(__file__).with_name("reset_pilot_environment.py")
    reset_contract = {
        "kind": "registered-script",
        "ref": "scripts/reset_pilot_environment.py",
        "sha256": digest(reset_script.read_bytes()),
    }
    retention = (datetime.now(UTC) + timedelta(days=180)).isoformat()
    acl_sha256 = digest({"tenant_id": environment["tenant_id"], "permission": "public-read"})
    assets = [
        publish_rag_task_bundle(
            store,
            {**case, "source": suite["source"]},
            tenant_id=environment["tenant_id"],
            environment_snapshot=snapshot,
            reset_contract=reset_contract,
            tool_contract=tool_contract,
            verifier_name="verify_rag_outcome",
            verifier_version=1,
            limits={"max_steps": 1, "deadline_seconds": 3600},
            acl_sha256=acl_sha256,
            permission_version="multidoc2dial-public-v3",
            retention_until=retention,
        )
        for case in suite["cases"]
    ]

    reset = execute_reset(environment, plan)
    checks, observations = preflight_environment(environment, snapshot)
    registry_sha256 = digest(args.registry.read_bytes())
    image_digest = runtime_image_digest(environment)
    receipt_map = {}
    for asset in assets:
        task_bundle_id = asset["fingerprint"]["task_bundle_id"]
        receipt, preflight = build_environment_receipt(
            environment,
            tenant_id=environment["tenant_id"],
            task_bundle_id=task_bundle_id,
            registry_sha256=registry_sha256,
            reset=reset,
            fixture_sha256=environment["fixture_sha256"],
            image_digest=image_digest,
            tool_contracts_sha256=tool.contract_digest,
            checks=checks,
            observations=observations,
        )
        published = publish_environment_receipt(
            store, receipt, preflight, tenant_id=environment["tenant_id"]
        )
        receipt_map[task_bundle_id] = {
            "bundle_ref": asset["fingerprint"]["task_bundle_ref"],
            "verifier_input_ref": asset["fingerprint"]["verifier_input_ref"],
            "receipt": {
                "ref": published["receipt_ref"],
                "sha256": published["receipt_sha256"],
            },
        }
    result = {
        "environment_id": environment["environment_id"],
        "source_sha256": environment["fixture_sha256"],
        "bundle_refs": [asset["fingerprint"]["task_bundle_ref"] for asset in assets],
        "receipt_map": receipt_map,
        "counts": {"cases": len(assets)},
        "registry_sha256": registry_sha256,
        "reset_plan_sha256": plan["plan_sha256"],
        "runtime_image_digest": image_digest,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cases": len(assets)}, sort_keys=True))


if __name__ == "__main__":
    main()
