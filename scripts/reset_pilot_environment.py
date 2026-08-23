"""Safely plan or reset one pre-registered test environment.

The default is dry-run.  Execution requires an exact environment record and
one-time confirmation derived from the rendered plan.  This script never
accepts production/shared targets or arbitrary shell commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.evidence import ObjectNotFound
from src.harness.experience import (
    publish_environment_receipt,
    validate_task_bundle,
)
from src.harness.experience import (
    task_bundle_id as compute_task_bundle_id,
)

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
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def load_environment(path: Path, environment_id: str) -> dict[str, Any]:  # noqa: C901
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
        "postgres_schema",
        "postgres_workload",
        "minio_bucket",
        "minio_prefix",
        "minio_workload",
        "redis_prefix",
        "redis_workload",
        "runtime_workload",
        "fixture_path",
        "fixture_sha256",
        "fixture_object",
        "restore_destination",
    )
    environment_id_value = str(environment.get("environment_id", "")).lower()
    if any(token in environment_id_value for token in _FORBIDDEN):
        raise ValueError("environment_target_forbidden:environment_id")
    if any(not isinstance(environment.get(key), str) or not environment[key] for key in required):
        raise ValueError("environment_registry_fields_missing")
    if environment["type"] != "test" or environment.get("reset_allowed") is not True:
        raise ValueError("environment_reset_not_allowed")
    for key in required:
        value = environment[key].lower()
        if any(token in value for token in _FORBIDDEN):
            raise ValueError(f"environment_target_forbidden:{key}")
    for key in (
        "environment_id",
        "tenant_id",
        "cluster",
        "namespace",
        "helm_release",
        "postgres_database",
        "postgres_schema",
        "postgres_workload",
        "minio_bucket",
        "minio_workload",
        "redis_workload",
        "runtime_workload",
    ):
        if not _IDENTIFIER.fullmatch(environment[key]):
            raise ValueError(f"environment_target_invalid:{key}")
    if not re.fullmatch(r"[0-9a-f]{64}", environment["fixture_sha256"]):
        raise ValueError("environment_fixture_hash_invalid")
    for key in ("minio_prefix", "fixture_object"):
        if not re.fullmatch(r"[a-zA-Z0-9._/-]+", environment[key]):
            raise ValueError(f"environment_target_invalid:{key}")
    if not environment["fixture_object"].startswith(environment["minio_prefix"]):
        raise ValueError("environment_fixture_outside_prefix")
    if not re.fullmatch(r"[a-zA-Z0-9:_-]+", environment["redis_prefix"]):
        raise ValueError("environment_target_invalid:redis_prefix")
    fixture = Path(environment["fixture_path"]).resolve()
    root = Path(__file__).resolve().parents[1]
    if not fixture.is_relative_to(root) or not fixture.is_file():
        raise ValueError("environment_fixture_path_invalid")
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != environment["fixture_sha256"]:
        raise ValueError("environment_fixture_hash_mismatch")
    if environment.get("source_permission_active") is not True:
        raise ValueError("environment_source_permission_revoked")
    return environment


def reset_plan(environment: dict[str, Any]) -> dict[str, Any]:
    plan = {
        "environment_id": environment["environment_id"],
        "kube_context": environment["kube_context"],
        "cluster": environment["cluster"],
        "namespace": environment["namespace"],
        "helm_release": environment["helm_release"],
        "postgres_database": environment["postgres_database"],
        "postgres_schema": environment["postgres_schema"],
        "minio": {"bucket": environment["minio_bucket"], "prefix": environment["minio_prefix"]},
        "redis_prefix": environment["redis_prefix"],
        "fixture_sha256": environment["fixture_sha256"],
        "actions": [
            "delete_labeled_kubernetes_jobs",
            "recreate_postgres_test_schema",
            "clear_minio_prefix",
            "clear_redis_prefix",
            "restore_pdf_rag_fixture",
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
    observations: dict[str, Any] | None = None,
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
            "postgres_schema",
            "minio_bucket",
            "minio_prefix",
            "fixture_object",
            "redis_prefix",
        )
    }
    preflight_evidence = {
        "schema_version": "environment_preflight.v1",
        "tenant_id": tenant_id,
        "environment_id": environment["environment_id"],
        "targets": targets,
        "checks": {name: checks[name] for name in _PREFLIGHT_CHECKS},
        "observations": observations or {},
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


def _kubectl(environment: dict[str, Any], *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            environment["kube_context"],
            "-n",
            environment["namespace"],
            *args,
        ],
        check=True,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _clear_postgres(environment: dict[str, Any]) -> None:
    schema = environment["postgres_schema"]
    tenant_id = environment["tenant_id"]
    fixture_sha256 = environment["fixture_sha256"]
    fixture_object = environment["fixture_object"]
    sql = f"""
DROP SCHEMA IF EXISTS {schema} CASCADE;
CREATE SCHEMA {schema};
CREATE TABLE {schema}.fixture_documents (
    tenant_id TEXT NOT NULL,
    fixture_sha256 TEXT NOT NULL,
    source_object TEXT NOT NULL,
    permission_active BOOLEAN NOT NULL
);
ALTER TABLE {schema}.fixture_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.fixture_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY fixture_tenant_policy ON {schema}.fixture_documents
USING (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA {schema} TO dataalchemy_app;
GRANT SELECT ON {schema}.fixture_documents TO dataalchemy_app;
INSERT INTO {schema}.fixture_documents VALUES (
    '{tenant_id}', '{fixture_sha256}', '{fixture_object}', true
);
""".encode()
    _postgres_admin(environment, sql)


def _postgres_admin(environment: dict[str, Any], sql: bytes) -> None:
    _kubectl(
        environment,
        "exec",
        "-i",
        f"statefulset/{environment['postgres_workload']}",
        "--",
        "sh",
        "-c",
        'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$1"',
        "--",
        environment["postgres_database"],
        input_bytes=sql,
    )


def _clear_minio(environment: dict[str, Any]) -> None:
    bucket = environment["minio_bucket"]
    prefix = environment["minio_prefix"].rstrip("/") + "/"
    _kubectl(
        environment,
        "exec",
        f"deployment/{environment['minio_workload']}",
        "--",
        "sh",
        "-c",
        'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
        '"$MINIO_ROOT_PASSWORD" >/dev/null && mc mb --ignore-existing "local/$1" >/dev/null '
        '&& mc rm --recursive --force "local/$1/$2" >/dev/null',
        "--",
        bucket,
        prefix,
    )


def _clear_redis(environment: dict[str, Any]) -> None:
    _kubectl(
        environment,
        "exec",
        f"deployment/{environment['redis_workload']}",
        "--",
        "sh",
        "-c",
        'redis-cli --scan --pattern "$1*" | while read -r key; do '
        'redis-cli DEL "$key" >/dev/null; done',
        "--",
        environment["redis_prefix"],
    )


def _restore_fixture(environment: dict[str, Any]) -> None:
    fixture = Path(environment["fixture_path"]).read_bytes()
    _kubectl(
        environment,
        "exec",
        "-i",
        f"deployment/{environment['minio_workload']}",
        "--",
        "sh",
        "-c",
        'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
        '"$MINIO_ROOT_PASSWORD" >/dev/null && mc pipe "local/$1/$2" >/dev/null',
        "--",
        environment["minio_bucket"],
        environment["fixture_object"],
        input_bytes=fixture,
    )
    _kubectl(
        environment,
        "exec",
        "-i",
        f"deployment/{environment['redis_workload']}",
        "--",
        "redis-cli",
        "-x",
        "SET",
        environment["redis_prefix"] + "fixture_sha256",
        input_bytes=environment["fixture_sha256"].encode(),
    )


def _postgres_count(environment: dict[str, Any], tenant_id: str) -> int:
    schema = environment["postgres_schema"]
    output = _kubectl(
        environment,
        "exec",
        "-i",
        f"statefulset/{environment['postgres_workload']}",
        "--",
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_APP_PASSWORD" psql -v ON_ERROR_STOP=1 -qAt '
        '-U dataalchemy_app -d "$1"',
        "--",
        environment["postgres_database"],
        input_bytes=(
            f"SET app.tenant_id = '{tenant_id}'; "
            f"SELECT count(*) FROM {schema}.fixture_documents "
            f"WHERE fixture_sha256 = '{environment['fixture_sha256']}';"
        ).encode(),
    )
    return int(output.decode().strip())


def _minio_fixture_sha256(environment: dict[str, Any]) -> str:
    try:
        output = _kubectl(
            environment,
            "exec",
            f"deployment/{environment['minio_workload']}",
            "--",
            "sh",
            "-c",
            'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
            '"$MINIO_ROOT_PASSWORD" >/dev/null && mc cat "local/$1/$2" | sha256sum',
            "--",
            environment["minio_bucket"],
            environment["fixture_object"],
        )
    except subprocess.CalledProcessError as error:
        message = error.stderr.decode(errors="replace").lower()
        if "not exist" in message or "not found" in message:
            return ""
        raise
    return output.decode().split()[0]


def _services_ready(environment: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    resources = [
        ("deployment", environment["minio_workload"]),
        ("deployment", environment["redis_workload"]),
        ("deployment", environment["runtime_workload"]),
        ("statefulset", environment["postgres_workload"]),
    ]
    ready: dict[str, bool] = {}
    for kind, name in resources:
        value = json.loads(_kubectl(environment, "get", kind, name, "-o", "json"))
        ready[name] = value.get("status", {}).get("readyReplicas", 0) == value["spec"]["replicas"]
    return all(ready.values()), ready


def preflight_environment(
    environment: dict[str, Any], task_environment: dict[str, Any]
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Probe the registered targets without exposing runtime credentials."""
    services_healthy, services = _services_ready(environment)
    minio_sha256 = _minio_fixture_sha256(environment)
    redis_sha256 = (
        _kubectl(
            environment,
            "exec",
            f"deployment/{environment['redis_workload']}",
            "--",
            "redis-cli",
            "--raw",
            "GET",
            environment["redis_prefix"] + "fixture_sha256",
        )
        .decode()
        .strip()
    )
    own_count = _postgres_count(environment, environment["tenant_id"])
    cross_tenant_count = _postgres_count(environment, "tve-non-target-tenant")
    fixture_present = (
        minio_sha256 == redis_sha256 == environment["fixture_sha256"] and own_count == 1
    )
    target_matches = (
        task_environment.get("environment_id") == environment["environment_id"]
        and task_environment.get("tenant_id") == environment["tenant_id"]
        and task_environment.get("fixture_sha256") == environment["fixture_sha256"]
    )
    checks = {
        "services_healthy": services_healthy,
        "fixture_present": fixture_present,
        "tenant_acl_readable": own_count == 1,
        "cross_tenant_denied": cross_tenant_count == 0,
        "target_matches_bundle": target_matches,
        "source_permission_active": (
            environment["source_permission_active"] is True
            and task_environment.get("source_permission_active") is True
        ),
    }
    observations = {
        "services": services,
        "fixture": {
            "postgres_rows": own_count,
            "minio_sha256": minio_sha256,
            "redis_sha256": redis_sha256,
        },
        "cross_tenant_rows": cross_tenant_count,
    }
    return checks, observations


def runtime_image_digest(environment: dict[str, Any]) -> str:
    deployment = json.loads(
        _kubectl(
            environment,
            "get",
            "deployment",
            environment["runtime_workload"],
            "-o",
            "json",
        )
    )
    selector = ",".join(
        f"{key}={value}" for key, value in deployment["spec"]["selector"]["matchLabels"].items()
    )
    pods = json.loads(_kubectl(environment, "get", "pods", "-l", selector, "-o", "json"))
    digests = {
        status["imageID"].rsplit("@", 1)[-1]
        for pod in pods["items"]
        if pod.get("status", {}).get("phase") == "Running"
        for status in pod["status"].get("containerStatuses", [])
        if status.get("ready")
        and status.get("imageID", "").rsplit("@", 1)[-1].startswith("sha256:")
    }
    if len(digests) != 1:
        raise RuntimeError("environment_runtime_image_digest_unavailable")
    return next(iter(digests))


class KubernetesMinioStore:
    """Use the registered MinIO pod without exporting its credentials."""

    def __init__(self, environment: dict[str, Any]):
        self.environment = environment

    def put(self, key: str, body: bytes) -> None:
        _kubectl(
            self.environment,
            "exec",
            "-i",
            f"deployment/{self.environment['minio_workload']}",
            "--",
            "sh",
            "-c",
            'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
            '"$MINIO_ROOT_PASSWORD" >/dev/null && mc mb --ignore-existing "local/$1" '
            '>/dev/null && mc pipe "local/$1/$2" >/dev/null',
            "--",
            self.environment["minio_bucket"],
            key,
            input_bytes=body,
        )

    def get(self, key: str) -> bytes:
        try:
            return _kubectl(
                self.environment,
                "exec",
                f"deployment/{self.environment['minio_workload']}",
                "--",
                "sh",
                "-c",
                'mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" '
                '"$MINIO_ROOT_PASSWORD" >/dev/null && mc cat "local/$1/$2"',
                "--",
                self.environment["minio_bucket"],
                key,
            )
        except subprocess.CalledProcessError as error:
            message = error.stderr.decode(errors="replace").lower()
            if "not exist" in message or "not found" in message:
                raise ObjectNotFound(key) from error
            raise


def execute_reset(environment: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Execute only the allowlisted test cleanup and return an immutable receipt.

    ponytail: external stores are cleared by their existing clients; add a
    separate restore workflow if pilot environments need partial retention.
    """
    _kubectl(
        environment,
        "delete",
        "jobs",
        "-l",
        f"dataalchemy.io/tve-environment={environment['environment_id']}",
        "--ignore-not-found=true",
    )
    _clear_postgres(environment)
    _clear_minio(environment)
    _clear_redis(environment)
    _restore_fixture(environment)
    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "plan_sha256": plan["plan_sha256"],
        "status": "reset_complete",
        "error_code": None,
    }
    return receipt


def execute_cleanup(environment: dict[str, Any]) -> dict[str, Any]:
    """Remove only this environment's labeled jobs, schema and object/key prefixes."""
    try:
        _kubectl(
            environment,
            "delete",
            "jobs",
            "-l",
            f"dataalchemy.io/tve-environment={environment['environment_id']}",
            "--ignore-not-found=true",
        )
        _postgres_admin(
            environment,
            f"DROP SCHEMA IF EXISTS {environment['postgres_schema']} CASCADE;".encode(),
        )
        _clear_minio(environment)
        _clear_redis(environment)
    except Exception as error:
        return {"status": "failed", "error_code": type(error).__name__}
    return {"status": "completed", "error_code": None}


def _load_task_environment(
    path: Path, bundle: dict[str, Any], environment: dict[str, Any]
) -> dict[str, Any]:
    task_environment = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "environment_id",
        "tenant_id",
        "fixture_sha256",
        "source_permission_active",
    }
    if (
        set(task_environment) != required
        or task_environment["schema_version"] != "tve_pdf_environment.v1"
    ):
        raise ValueError("task_environment_snapshot_invalid")
    body = json.dumps(
        task_environment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(body).hexdigest() != bundle["environment"]["snapshot_sha256"]:
        raise ValueError("task_environment_snapshot_hash_mismatch")
    if bundle["environment"]["snapshot_tenant_id"] != environment["tenant_id"]:
        raise ValueError("task_environment_tenant_mismatch")
    return task_environment


def main() -> None:  # noqa: C901 - one fail-closed reset/preflight sequence
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry", type=Path, default=Path("deploy/pilot-environments.example.yaml")
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--task-bundle", type=Path)
    parser.add_argument("--task-environment", type=Path)
    parser.add_argument("--tool-contracts-sha256")
    parser.add_argument("--receipt-output", type=Path)
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
    if not args.task_bundle or not args.task_environment or not args.receipt_output:
        raise SystemExit("task_bundle_environment_and_receipt_output_required")
    if not re.fullmatch(r"[0-9a-f]{64}", args.tool_contracts_sha256 or ""):
        raise SystemExit("tool_contracts_sha256_required")
    bundle = validate_task_bundle(json.loads(args.task_bundle.read_text(encoding="utf-8")))
    task_environment = _load_task_environment(args.task_environment, bundle, environment)
    registry_sha256 = hashlib.sha256(args.registry.read_bytes()).hexdigest()
    image_digest = runtime_image_digest(environment)
    failure: Exception | None = None
    try:
        reset = execute_reset(environment, plan)
    except Exception as error:
        failure = error
        reset = {
            "receipt_id": str(uuid.uuid4()),
            "plan_sha256": plan["plan_sha256"],
            "status": "reset_failed",
            "error_code": type(error).__name__,
        }
        checks = dict.fromkeys(_PREFLIGHT_CHECKS, False)
        observations = {"reset_error": type(error).__name__}
    else:
        try:
            checks, observations = preflight_environment(environment, task_environment)
        except Exception as error:
            failure = error
            checks = dict.fromkeys(_PREFLIGHT_CHECKS, False)
            observations = {"preflight_error": type(error).__name__}
    receipt, preflight = build_environment_receipt(
        environment,
        tenant_id=environment["tenant_id"],
        task_bundle_id=compute_task_bundle_id(bundle),
        registry_sha256=registry_sha256,
        reset=reset,
        fixture_sha256=environment["fixture_sha256"],
        image_digest=image_digest,
        tool_contracts_sha256=args.tool_contracts_sha256,
        checks=checks,
        observations=observations,
    )
    try:
        published = publish_environment_receipt(
            KubernetesMinioStore(environment),
            receipt,
            preflight,
            tenant_id=environment["tenant_id"],
        )
    except Exception as error:
        if failure is None:
            raise
        published = {"error_code": type(error).__name__}
    output = {**receipt, "publication": published}
    args.receipt_output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt_output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))
    if failure is not None:
        raise failure
    if receipt["state"] != "ready":
        raise RuntimeError(f"environment_preflight_failed:{receipt['invalid_reason']}")


if __name__ == "__main__":
    main()
