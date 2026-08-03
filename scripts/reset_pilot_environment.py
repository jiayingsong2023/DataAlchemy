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
        "environment_id", "type", "kube_context", "cluster", "namespace", "helm_release",
        "postgres_database", "minio_bucket", "minio_prefix", "redis_prefix", "restore_destination",
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
        "actions": ["delete_kubernetes_jobs", "clear_postgres_test_schema", "clear_minio_prefix", "clear_redis_prefix"],
    }
    plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan["plan_sha256"] = hashlib.sha256(plan_json.encode()).hexdigest()
    return plan


def _kubectl(environment: dict[str, Any], *args: str) -> None:
    subprocess.run(
        ["kubectl", "--context", environment["kube_context"], "-n", environment["namespace"], *args],
        check=True,
    )


def _clear_postgres(environment: dict[str, Any]) -> None:
    database_url = os.getenv("PILOT_RESET_DATABASE_URL")
    if not database_url:
        raise RuntimeError("PILOT_RESET_DATABASE_URL_required")
    import psycopg

    tables = (
        "pilot_evidence_records", "pilot_programs", "qualification_records",
        "release_records", "adapter_manifests", "training_snapshot_items", "training_snapshots",
        "trajectory_annotations", "trajectory_trials", "evaluation_campaigns",
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
        "s3", endpoint_url=os.environ["PILOT_RESET_S3_ENDPOINT"],
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
        "environment_id": environment["environment_id"],
        "plan_sha256": plan["plan_sha256"],
        "executor": os.getenv("USER", "unknown"),
        "status": "reset_complete",
    }
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=Path("deploy/pilot-environments.example.yaml"))
    parser.add_argument("--environment", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    environment = load_environment(args.registry, args.environment)
    plan = reset_plan(environment)
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "plan": plan}, ensure_ascii=False, indent=2))
    if not args.execute:
        return
    expected = f"reset:{args.environment}:{plan['plan_sha256'][:12]}"
    if args.confirm != expected:
        raise SystemExit(f"confirmation_required:{expected}")
    print(json.dumps(execute_reset(environment, plan), ensure_ascii=False))


if __name__ == "__main__":
    main()
