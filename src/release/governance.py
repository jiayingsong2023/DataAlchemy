"""Approval and rollback state machine for model, prompt and tool releases."""

from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any

from storage.audit import AuditLog
from storage.postgres import PostgresDatabase


class ReleaseGovernance:
    transitions = {
        "candidate": {"shadow", "rejected"},
        "shadow": {"canary", "rejected"},
        "canary": {"promoted", "rolled_back"},
        "promoted": {"rolled_back"},
        "rejected": set(),
        "rolled_back": set(),
    }

    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)

    def create_candidate(self, identity: dict[str, str], manifest: dict[str, Any]) -> str:
        self._admin(identity)
        manifest = dict(manifest)
        required = {"code_version", "evaluation", "rollback_to"}
        if missing := required - manifest.keys():
            raise ValueError(f"Release manifest missing: {', '.join(sorted(missing))}")
        if manifest["evaluation"].get("passed") is not True:
            raise ValueError("Release evaluation must pass before shadowing")
        h5 = manifest.get("harness_version") == 5
        if h5:
            required_h5 = {
                "adapter_id",
                "evaluation_id",
                "training_snapshot_id",
                "guardrails",
                "release_scope",
                "approvals",
            }
            if missing := required_h5 - manifest.keys():
                raise ValueError(f"H5 release manifest missing: {', '.join(sorted(missing))}")
            if manifest["release_scope"] != "single_tenant_lora":
                raise ValueError("Unsupported H5 release scope")
            if manifest.get("approvals", {}).get("candidate") != identity["username"]:
                raise PermissionError("Candidate approval actor mismatch")
            if manifest["approvals"].get("promote") == identity["username"]:
                raise PermissionError("Candidate creator cannot promote release")
            guardrails = manifest["guardrails"]
            for key in ("max_error_rate", "max_p95_ms", "min_samples", "window_seconds"):
                if key not in guardrails:
                    raise ValueError(f"H5 guardrail missing: {key}")
            if manifest.get("evaluation", {}).get("hard_gates_passed") is not True:
                raise ValueError("H5 hard gates must pass before shadowing")
            if not isinstance(manifest["rollback_to"], str) or not manifest["rollback_to"]:
                raise ValueError("H5 rollback target missing")
            manifest.setdefault("created_by", identity["username"])
        release_id = str(uuid.uuid4())
        manifest_sha256 = None
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                if h5:
                    if manifest["rollback_to"] != "base":
                        cursor.execute(
                            "SELECT release_id FROM release_records WHERE release_id = %s AND tenant_id = %s "
                            "AND status = 'promoted' AND release_scope = %s",
                            (manifest["rollback_to"], identity["tenant_id"], manifest["release_scope"]),
                        )
                        if cursor.fetchone() is None:
                            raise ValueError("H5 rollback target must be an active promoted release or base")
                    cursor.execute(
                        "SELECT a.state AS adapter_state, s.state AS snapshot_state, e.state AS evaluation_state "
                        "FROM adapter_manifests a JOIN training_snapshots s ON s.snapshot_id = a.snapshot_id "
                        "JOIN evaluation_campaigns e ON e.evaluation_id = %s "
                        "WHERE a.adapter_id = %s AND a.tenant_id = %s AND s.snapshot_id = %s",
                        (
                            manifest["evaluation_id"],
                            manifest["adapter_id"],
                            identity["tenant_id"],
                            manifest["training_snapshot_id"],
                        ),
                    )
                    refs = cursor.fetchone()
                    if refs is None or refs["adapter_state"] != "verified" or refs["snapshot_state"] != "approved" or refs["evaluation_state"] != "passed":
                        raise ValueError("H5 release dependencies are not verified")
                    manifest_sha256 = hashlib.sha256(
                        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    cursor.execute(
                        "INSERT INTO release_records "
                        "(release_id, tenant_id, status, manifest_json, release_kind, release_scope, adapter_id, "
                        "evaluation_id, training_snapshot_id, rollback_release_id, policy_version, manifest_sha256, qualification_id) "
                        "VALUES (%s, %s, 'candidate', %s::jsonb, 'model', %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            release_id,
                            identity["tenant_id"],
                            json.dumps(manifest, ensure_ascii=False),
                            manifest["release_scope"],
                            manifest["adapter_id"],
                            manifest["evaluation_id"],
                            manifest["training_snapshot_id"],
                            None if manifest["rollback_to"] == "base" else manifest["rollback_to"],
                            manifest.get("policy_version"),
                            manifest_sha256,
                            manifest.get("qualification_id"),
                        ),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO release_records (release_id, tenant_id, status, manifest_json) "
                        "VALUES (%s, %s, 'candidate', %s::jsonb)",
                        (release_id, identity["tenant_id"], json.dumps(manifest, ensure_ascii=False)),
                    )
        self.audit.record(
            identity,
            "release.candidate",
            "release",
            resource_id=release_id,
            correlation_id=release_id,
            metadata={"harness_version": manifest.get("harness_version"), "manifest_sha256": manifest_sha256},
        )
        return release_id

    def advance(
        self,
        release_id: str,
        target: str,
        identity: dict[str, str],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        self._admin(identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM release_records WHERE release_id = %s FOR UPDATE", (release_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Release not found")
                if target not in self.transitions[row["status"]]:
                    raise ValueError(f"Invalid release transition: {row['status']} → {target}")
                manifest = row["manifest_json"]
                h5 = manifest.get("harness_version") == 5
                if h5 and target == "promoted":
                    if manifest.get("approvals", {}).get("promote") != identity["username"]:
                        raise PermissionError("Independent promote approval required")
                    if manifest.get("created_by") == identity["username"]:
                        raise PermissionError("Candidate creator cannot promote release")
                next_version = int(row.get("version") or 1) + 1
                cursor.execute(
                    "UPDATE release_records SET status = %s, approved_by = %s, updated_at = now(), version = %s "
                    "WHERE release_id = %s AND version = %s",
                    (target, identity["username"], next_version, release_id, expected_version or row.get("version") or 1),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Release version conflict")
                row["status"] = target
                row["approved_by"] = identity["username"]
                row["version"] = next_version
        self.audit.record(
            identity,
            f"release.{target}",
            "release",
            resource_id=release_id,
            correlation_id=release_id,
            metadata={"target": target, "version": row.get("version")},
        )
        return {**row, "release_id": str(row["release_id"])}

    def observe(
        self,
        release_id: str,
        metrics: dict[str, float],
        identity: dict[str, str],
        *,
        promote: bool = True,
    ) -> str:
        """Promote only a healthy canary; breach configured error/latency limits rolls it back."""
        self._admin(identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM release_records WHERE release_id = %s", (release_id,))
                row = cursor.fetchone()
        if row is None or row["status"] != "canary":
            raise PermissionError("Canary release not found")
        guardrails = row["manifest_json"].get("guardrails", {})
        if row["manifest_json"].get("harness_version") == 5:
            required = {"sample_count", "window_seconds", "security_passed", "window_complete"}
            if not required <= metrics.keys():
                raise ValueError("H5 canary observation is incomplete")
            if metrics["sample_count"] < guardrails["min_samples"] or metrics["window_seconds"] < guardrails["window_seconds"]:
                return "canary"
            if metrics["security_passed"] is not True or metrics["window_complete"] is not True:
                return self.advance(release_id, "rolled_back", identity)["status"]
        breached = (
            "error_rate" not in metrics
            or "p95_ms" not in metrics
            or metrics["error_rate"] > guardrails.get("max_error_rate", 1.0)
            or metrics["p95_ms"] > guardrails.get("max_p95_ms", float("inf"))
        )
        if not breached and not promote:
            self.audit.record(
                identity,
                "release.canary_observed",
                "release",
                resource_id=release_id,
                correlation_id=release_id,
                metadata=metrics,
            )
            return "awaiting_promotion"
        target = "rolled_back" if breached else "promoted"
        return self.advance(release_id, target, identity)["status"]

    def status(self, release_id: str, identity: dict[str, str]) -> str | None:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM release_records WHERE release_id = %s AND tenant_id = %s",
                    (release_id, identity["tenant_id"]),
                )
                row = cursor.fetchone()
        return row["status"] if row else None

    @staticmethod
    def _admin(identity: dict[str, str]) -> None:
        if identity["role"] != "admin":
            raise PermissionError("Administrator role required")
