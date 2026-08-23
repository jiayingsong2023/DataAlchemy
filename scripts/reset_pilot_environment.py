"""Safely plan or reset one pre-registered test environment.

The default is dry-run.  Execution requires an exact environment record and
one-time confirmation derived from the rendered plan.  This script never
accepts production/shared targets or arbitrary shell commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml

_FORBIDDEN = ("prod", "production", "shared", "root", "*", "..")
_PREFLIGHT_CHECKS = (
    "services_healthy",
    "fixture_present",
    "tenant_acl_readable",
    "cross_tenant_denied",
    "target_matches_bundle",
    "source_permission_active",
)
_PREFLIGHT_FAILURES = {
    "services_healthy": "service_unavailable",
    "fixture_present": "fixture_missing",
    "tenant_acl_readable": "tenant_acl_unreadable",
    "cross_tenant_denied": "cross_tenant_access_allowed",
    "target_matches_bundle": "reset_target_mismatch",
    "source_permission_active": "source_permission_revoked",
}


def load_environment(path: Path, environment_id: str) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    environments = payload.get("environments")
    if not isinstance(environments, list):
        raise ValueError("environment_registry_invalid")
    matches = [item for item in environments if item.get("environment_id") == environment_id]
    if len(matches) != 1:
        raise ValueError("environment_not_registered")
    environment = matches[0]
    required = (
        "environment_id",
        "tenant_id",
        "type",
        "kube_context",
        "cluster",
        "namespace",
        "helm_release",
        "postgres_database",
        "minio_bucket",
        "minio_prefix",
        "redis_prefix",
        "restore_destination",
    )
    if any(not isinstance(environment.get(key), str) or not environment[key] for key in required):
        raise ValueError("environment_registry_fields_missing")
    if environment["type"] != "test" or environment.get("reset_allowed") is not True:
        raise ValueError("environment_reset_not_allowed")
    for key in required:
        value = environment[key].lower()
        if any(token in value for token in _FORBIDDEN):
            raise ValueError(f"environment_target_forbidden:{key}")
    return environment


def reset_plan(environment: dict[str, Any]) -> dict[str, Any]:
    plan = {
        "environment_id": environment["environment_id"],
        "kube_context": environment["kube_context"],
        "cluster": environment["cluster"],
        "namespace": environment["namespace"],
        "helm_release": environment["helm_release"],
        "postgres_database": environment["postgres_database"],
        "minio": {"bucket": environment["minio_bucket"], "prefix": environment["minio_prefix"]},
        "redis_prefix": environment["redis_prefix"],
        "actions": [
            "delete_kubernetes_jobs",
            "clear_postgres_test_schema",
            "clear_minio_prefix",
            "clear_redis_prefix",
        ],
    }
    plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan["plan_sha256"] = hashlib.sha256(plan_json.encode()).hexdigest()
    return plan


def build_environment_receipt(
    environment: dict[str, Any],
    *,
    tenant_id: str,
    task_bundle_id: str,
    registry_sha256: str,
    reset: dict[str, Any],
    fixture_sha256: str,
    image_digest: str,
    tool_contracts_sha256: str,
    checks: dict[str, bool],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build deterministic preflight evidence and a v1 environment receipt."""
    if environment.get("tenant_id") != tenant_id:
        raise ValueError("environment_tenant_mismatch")
    if set(checks) != set(_PREFLIGHT_CHECKS) or any(
        type(value) is not bool for value in checks.values()
    ):
        raise ValueError("environment_preflight_checks_invalid")
    targets = {
        key: environment[key]
        for key in (
            "kube_context",
            "cluster",
            "namespace",
            "postgres_database",
            "minio_bucket",
            "minio_prefix",
            "redis_prefix",
        )
    }
    preflight_evidence = {
        "schema_version": "environment_preflight.v1",
        "tenant_id": tenant_id,
        "environment_id": environment["environment_id"],
        "targets": targets,
        "checks": {name: checks[name] for name in _PREFLIGHT_CHECKS},
    }
    preflight_body = json.dumps(
        preflight_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    preflight_sha256 = hashlib.sha256(preflight_body).hexdigest()
    preflight_ref = f"tenants/{tenant_id}/environment-evidence/preflight/{preflight_sha256}.json"
    failure = next((name for name in _PREFLIGHT_CHECKS if not checks[name]), None)
    ready = reset["status"] == "reset_complete" and failure is None
    initial_state = {
        "task_bundle_id": task_bundle_id,
        "registry_sha256": registry_sha256,
        "plan_sha256": reset["plan_sha256"],
        "fixture_sha256": fixture_sha256,
        "runtime": {
            "image_digest": image_digest,
            "tool_contracts_sha256": tool_contracts_sha256,
        },
        "targets": targets,
        "checks": preflight_evidence["checks"],
    }
    initial_state_sha256 = hashlib.sha256(
        json.dumps(initial_state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    error_code = reset.get("error_code") or (_PREFLIGHT_FAILURES[failure] if failure else None)
    receipt = {
        "schema_version": "environment_receipt.v1",
        "task_bundle_id": task_bundle_id,
        "environment_id": environment["environment_id"],
        "registry_sha256": registry_sha256,
        "reset": reset,
        "fixture_sha256": fixture_sha256,
        "runtime": {
            "image_digest": image_digest,
            "tool_contracts_sha256": tool_contracts_sha256,
        },
        "preflight": {
            "status": "passed" if ready else "failed",
            "error_code": None if ready else error_code,
            "evidence_refs": [{"ref": preflight_ref, "sha256": preflight_sha256}],
        },
        "initial_state_sha256": initial_state_sha256 if ready else None,
        "final_state_delta_sha256": None,
        "cleanup": {"status": "not_started", "error_code": None},
        "state": "ready" if ready else "invalidated",
        "invalid_reason": None if ready else error_code,
    }
    return receipt, preflight_evidence


def _kubectl(environment: dict[str, Any], *args: str) -> None:
    subprocess.run(
        [
            "kubectl",
            "--context",
            environment["kube_context"],
            "-n",
            environment["namespace"],
            *args,
        ],
        check=True,
    )


def _clear_postgres(environment: dict[str, Any]) -> None:
    database_url = os.getenv("PILOT_RESET_DATABASE_URL")
    if not database_url:
        raise RuntimeError("PILOT_RESET_DATABASE_URL_required")
    import psycopg

    tables = (
        "pilot_evidence_records",
        "pilot_programs",
        "qualification_records",
        "release_records",
        "adapter_manifests",
        "training_snapshot_items",
        "training_snapshots",
        "trajectory_annotations",
        "trajectory_trials",
        "evaluation_campaigns",
    )
    with psycopg.connect(database_url) as connection:
        if connection.info.dbname != environment["postgres_database"]:
            raise RuntimeError("reset_database_target_mismatch")
        with connection.cursor() as cursor:
            cursor.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
        connection.commit()


def _clear_minio(environment: dict[str, Any]) -> None:
    import boto3
    from botocore.client import Config

    bucket = environment["minio_bucket"]
    prefix = environment["minio_prefix"].rstrip("/") + "/"
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["PILOT_RESET_S3_ENDPOINT"],
        aws_access_key_id=os.environ["PILOT_RESET_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["PILOT_RESET_S3_SECRET_KEY"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


def _clear_redis(environment: dict[str, Any]) -> None:
    import redis

    client = redis.Redis.from_url(os.environ["PILOT_RESET_REDIS_URL"])
    prefix = environment["redis_prefix"]
    keys = list(client.scan_iter(match=prefix + "*"))
    if keys:
        client.delete(*keys)


def execute_reset(environment: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Execute only the allowlisted test cleanup and return an immutable receipt.

    ponytail: external stores are cleared by their existing clients; add a
    separate restore workflow if pilot environments need partial retention.
    """
    _kubectl(environment, "delete", "jobs", "--all", "--ignore-not-found=true")
    _clear_postgres(environment)
    _clear_minio(environment)
    _clear_redis(environment)
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "plan_sha256": plan["plan_sha256"],
        "status": "reset_complete",
        "error_code": None,
    }
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry", type=Path, default=Path("deploy/pilot-environments.example.yaml")
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    environment = load_environment(args.registry, args.environment)
    plan = reset_plan(environment)
    print(
        json.dumps(
            {"mode": "execute" if args.execute else "dry-run", "plan": plan},
            ensure_ascii=False,
            indent=2,
        )
    )
    if not args.execute:
        return
    expected = f"reset:{args.environment}:{plan['plan_sha256'][:12]}"
    if args.confirm != expected:
        raise SystemExit(f"confirmation_required:{expected}")
    try:
        receipt = execute_reset(environment, plan)
    except Exception as error:
        receipt = {
            "receipt_id": str(uuid.uuid4()),
            "plan_sha256": plan["plan_sha256"],
            "status": "reset_failed",
            "error_code": type(error).__name__,
        }
        print(json.dumps(receipt, ensure_ascii=False))
        raise
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
