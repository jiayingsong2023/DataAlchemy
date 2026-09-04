"""Run the RTD3 revocation rehearsal in a disposable tenant."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from connectors.git import GitConnector
from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from harness.evaluation import EvaluationService
from harness.experience import _put_immutable
from rag.vector_store import VectorStore
from release.governance import ReleaseGovernance
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils


def _identity(tenant: str, username: str, role: str) -> dict[str, str]:
    return {"tenant_id": tenant, "username": username, "role": role}


def _seed(database: PostgresDatabase, identity: dict[str, str], marker: str) -> dict[str, Any]:
    """Create disposable RAG and governed-training rows, never production data."""
    document_id = str(uuid.uuid4())
    source_uri = f"github://rtd3/rehearsal/commit/{uuid.uuid4()}"
    chains: dict[str, dict[str, Any]] = {}
    task_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    with database.transaction(identity) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO documents (document_id, tenant_id, owner_id, source_uri, content_hash, "
                "status, metadata_json) VALUES (%s, %s, %s, %s, %s, 'ready', '{}'::jsonb)",
                (
                    document_id,
                    identity["tenant_id"],
                    identity["username"],
                    source_uri,
                    sha256(marker.encode()),
                ),
            )
            cursor.execute(
                "INSERT INTO document_acl (document_id, tenant_id, subject_type, subject_id, permission) "
                "VALUES (%s, %s, 'user', %s, 'admin'), (%s, %s, 'tenant', %s, 'read')",
                (
                    document_id,
                    identity["tenant_id"],
                    identity["username"],
                    document_id,
                    identity["tenant_id"],
                    identity["tenant_id"],
                ),
            )
            cursor.execute(
                "INSERT INTO document_chunks (chunk_id, document_id, ordinal, text, lexemes, fts, "
                "embedding, metadata_json) VALUES (%s, %s, 0, %s, %s, "
                "to_tsvector('simple', %s), %s::vector, '{}'::jsonb)",
                (
                    str(uuid.uuid4()),
                    document_id,
                    marker,
                    marker,
                    marker,
                    "[" + ",".join(["0"] * 512) + "]",
                ),
            )
            cursor.execute(
                "INSERT INTO agent_tasks (task_id, run_id, tenant_id, owner, role, goal, state, "
                "plan_json, max_steps, budget_json) VALUES (%s, %s, %s, %s, 'admin', %s, "
                "'succeeded', '[]'::jsonb, 1, '{}'::jsonb)",
                (task_id, run_id, identity["tenant_id"], identity["username"], "RTD3 fixture"),
            )
            selectors = {
                "source_acl": {"source_acl_digest": sha256(b"rtd3-acl")},
                "permission": {"permission_version": "rtd3-permission-v1"},
                "source": {"source_version": f"sha256:{sha256(b'rtd3-source')}"},
            }
            for index, (name, selector) in enumerate(selectors.items()):
                annotation_id = str(uuid.uuid4())
                snapshot_id = str(uuid.uuid4())
                adapter_id = str(uuid.uuid4())
                release_id = str(uuid.uuid4())
                source_digest = sha256(f"rtd3-{name}".encode())
                acl_digest = selector.get("source_acl_digest", sha256(f"rtd3-{name}-acl".encode()))
                permission = selector.get("permission_version", f"rtd3-{name}-permission-v1")
                source_version = selector.get("source_version", f"sha256:{source_digest}")
                cursor.execute(
                    "INSERT INTO trajectory_annotations (annotation_id, run_id, tenant_id, kind, "
                    "label_json, content_key, content_sha256, source_acl_digest, training_allowed, "
                    "training_purpose, training_permission_version, reviewer, status, reviewed_at) "
                    "VALUES (%s, %s, %s, 'human_review', %s::jsonb, %s, %s, %s, true, "
                    "'model_improvement', %s, 'rtd3-reviewer', 'approved', now())",
                    (
                        annotation_id,
                        run_id,
                        identity["tenant_id"],
                        json.dumps({"evidence_refs": [{"source_version": source_version}]}),
                        f"tenants/{identity['tenant_id']}/rtd3/{name}.json",
                        source_digest,
                        acl_digest,
                        permission,
                    ),
                )
                cursor.execute(
                    "INSERT INTO training_snapshots (snapshot_id, tenant_id, created_by, state, "
                    "dataset_key, dataset_sha256, dataset_size, policy_version, split_json, "
                    "base_model_digest, approved_by, approved_at) VALUES (%s, %s, %s, 'approved', "
                    "%s, %s, 1, 'rtd3-v1', %s::jsonb, %s, 'rtd3-reviewer', now())",
                    (
                        snapshot_id,
                        identity["tenant_id"],
                        identity["username"],
                        f"tenants/{identity['tenant_id']}/rtd3/{name}.jsonl",
                        sha256(f"dataset-{name}".encode()),
                        json.dumps({"train": int(index != 2), "validation": int(index == 2)}),
                        sha256(b"rtd3-base-model"),
                    ),
                )
                cursor.execute(
                    "INSERT INTO training_snapshot_items (snapshot_id, item_id, split, source_type, "
                    "source_id, source_tenant_id, source_sha256, source_acl_digest, training_allowed, "
                    "training_purpose, training_permission_version) VALUES (%s, %s, %s, "
                    "'trajectory_annotation', %s, %s, %s, %s, true, 'model_improvement', %s)",
                    (
                        snapshot_id,
                        f"rtd3-{name}",
                        "validation" if index == 2 else "train",
                        annotation_id,
                        identity["tenant_id"],
                        source_digest,
                        acl_digest,
                        permission,
                    ),
                )
                cursor.execute(
                    "INSERT INTO adapter_manifests (adapter_id, tenant_id, snapshot_id, "
                    "base_model_digest, tokenizer_digest, artifact_key, artifact_sha256, artifact_size, "
                    "config_json, environment_json, safety_scan_json, state) VALUES (%s, %s, %s, %s, "
                    "%s, %s, %s, 1, '{\"format\":\"safetensors\"}'::jsonb, '{}'::jsonb, "
                    "'{\"passed\":true}'::jsonb, 'verified')",
                    (
                        adapter_id,
                        identity["tenant_id"],
                        snapshot_id,
                        sha256(b"rtd3-base-model"),
                        sha256(b"rtd3-tokenizer"),
                        f"tenants/{identity['tenant_id']}/rtd3/{name}.safetensors",
                        sha256(f"adapter-{name}".encode()),
                    ),
                )
                cursor.execute(
                    "INSERT INTO release_records (release_id, tenant_id, status, manifest_json, "
                    "release_kind, release_scope, adapter_id, training_snapshot_id, policy_version, "
                    "manifest_sha256) VALUES (%s, %s, 'promoted', %s::jsonb, 'model', "
                    "'single_tenant_lora', %s, %s, 'rtd3-v1', %s)",
                    (
                        release_id,
                        identity["tenant_id"],
                        json.dumps({"harness_version": 5, "selector": selector}),
                        adapter_id,
                        snapshot_id,
                        sha256(canonical_bytes(selector)),
                    ),
                )
                chains[name] = {
                    "selector": selector,
                    "annotation_id": annotation_id,
                    "snapshot_id": snapshot_id,
                    "adapter_id": adapter_id,
                    "release_id": release_id,
                }
    return {"document_id": document_id, "source_uri": source_uri, "chains": chains}


def _states(
    database: PostgresDatabase, identity: dict[str, str], chain: dict[str, Any]
) -> dict[str, str]:
    with database.transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            result = {}
            for key, table, column, identifier in (
                ("annotation", "trajectory_annotations", "status", chain["annotation_id"]),
                ("snapshot", "training_snapshots", "state", chain["snapshot_id"]),
                ("adapter", "adapter_manifests", "state", chain["adapter_id"]),
                ("release", "release_records", "status", chain["release_id"]),
            ):
                cursor.execute(
                    f"SELECT {column} AS state FROM {table} WHERE {key}_id = %s", (identifier,)
                )
                result[key] = cursor.fetchone()["state"]
    return result


def _revoke_training_chains(
    evaluation: EvaluationService,
    database: PostgresDatabase,
    owner: dict[str, str],
    chains: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    propagation = {}
    releases = ReleaseGovernance(database.database_url)
    for name, chain in chains.items():
        expected = {
            "annotations": [chain["annotation_id"]],
            "snapshots": [chain["snapshot_id"]],
            "adapters": [chain["adapter_id"]],
            "releases": [chain["release_id"]],
        }
        impact = evaluation.source_impact(owner, **chain["selector"])
        if impact != expected:
            raise RuntimeError(f"rtd3_impact_mismatch:{name}:{impact}")
        revoked = evaluation.revoke_source(
            owner, reason=f"RTD3 {name} revocation", **chain["selector"]
        )
        states = _states(database, owner, chain)
        if revoked != expected or states != {
            "annotation": "revoked",
            "snapshot": "revoked",
            "adapter": "revoked",
            "release": "rolled_back",
        }:
            raise RuntimeError(f"rtd3_propagation_failed:{name}:{revoked}:{states}")
        try:
            evaluation.create_adapter_candidate(
                owner,
                snapshot_id=chain["snapshot_id"],
                base_model_digest=sha256(b"rtd3-base-model"),
                tokenizer_digest=sha256(b"rtd3-tokenizer"),
                artifact_key="blocked.safetensors",
                artifact_sha256=sha256(b"blocked"),
                artifact_size=1,
                config={"format": "safetensors"},
                environment={},
                safety_scan={"passed": True},
            )
        except ValueError as error:
            blocked = str(error) == "snapshot_not_approved"
        else:
            blocked = False
        if not blocked:
            raise RuntimeError(f"rtd3_adapter_not_blocked:{name}")
        try:
            releases.advance(chain["release_id"], "promoted", owner)
        except ValueError as error:
            repromotion_blocked = "Invalid release transition" in str(error)
        else:
            repromotion_blocked = False
        if not repromotion_blocked:
            raise RuntimeError(f"rtd3_release_repromotion_not_blocked:{name}")
        propagation[name] = {
            "impact": impact,
            "states": states,
            "new_adapter_blocked": blocked,
            "release_repromotion_blocked": repromotion_blocked,
        }
    return propagation


def run(database_url: str, tenant: str) -> dict[str, Any]:
    if not tenant.startswith(("rtd3-rehearsal-", "rtd-q3-")):
        raise ValueError("rtd3_tenant_prefix_required")
    build_sha = os.getenv("BUILD_GIT_SHA")
    if not build_sha or build_sha == "unknown":
        raise RuntimeError("rtd3_build_git_sha_missing")
    database = PostgresDatabase(database_url)
    owner = _identity(tenant, "rtd3-owner", "admin")
    reader = _identity(tenant, "rtd3-reader", "user")
    other = _identity(f"rtd3-cross-tenant-{uuid.uuid4()}", "rtd3-reader", "user")
    marker = f"rtd3marker{uuid.uuid4().hex}"
    seeded = _seed(database, owner, marker)
    vector_store = VectorStore(database_url=database_url)
    evaluation = EvaluationService(database_url)

    rag = {
        "visible_before_acl_revoke": bool(vector_store.search_text(marker, reader)),
        "cross_tenant_visible": bool(vector_store.search_text(marker, other)),
    }
    vector_store.replace_acl([seeded["document_id"]], [], owner)
    rag["visible_after_acl_revoke"] = bool(vector_store.search_text(marker, reader))
    rag["source_rows_revoked"] = GitConnector(database_url, "rtd3/rehearsal").revoke_source(
        seeded["source_uri"], owner
    )
    rag["visible_after_source_revoke"] = bool(vector_store.search_text(marker, owner))
    if rag != {
        "visible_before_acl_revoke": True,
        "cross_tenant_visible": False,
        "visible_after_acl_revoke": False,
        "source_rows_revoked": 1,
        "visible_after_source_revoke": False,
    }:
        raise RuntimeError(f"rtd3_rag_revocation_failed:{rag}")

    propagation = _revoke_training_chains(evaluation, database, owner, seeded["chains"])

    with database.transaction(owner, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS contamination FROM (SELECT source_id FROM training_snapshot_items "
                "GROUP BY source_id HAVING count(DISTINCT split) > 1) contaminated"
            )
            split_contamination = int(cursor.fetchone()["contamination"])
    if split_contamination:
        raise RuntimeError("rtd3_split_contamination")
    return {
        "schema_version": "rtd3_revocation_rehearsal.v1",
        "decision": "PASS",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant,
        "build_git_sha": build_sha,
        "rag": rag,
        "propagation": propagation,
        "split_contamination": split_contamination,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=f"rtd3-rehearsal-{uuid.uuid4()}")
    args = parser.parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    report = run(database_url, args.tenant)
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{args.tenant}/evaluations/revocation-rehearsal/sha256/{digest}.json"
    s3 = S3Utils()
    s3.ensure_bucket()
    _put_immutable(S3EvidenceStore(s3.bucket, s3.client), ref, body)
    print(
        json.dumps(
            {"decision": "PASS", "receipt_ref": ref, "receipt_sha256": digest, "report": report},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
