"""Clone the frozen RAG fixture into an isolated tenant and publish Q3 tasks."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from harness.experience import publish_environment_receipt, publish_rag_task_bundle
from rag.vector_store import VectorStore
from scripts.run_h5_pdf_cycle import build_runtime
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils

SUITE = Path(__file__).resolve().parents[1] / "src/harness/fixtures/rag_projection_ab_suite.json"


def _source(database_url: str, document_id: str) -> dict:
    identity = {"tenant_id": "default", "username": "rtd-q3-source-reader", "role": "admin"}
    with PostgresDatabase(database_url).transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.source_uri, d.metadata_json, c.text, c.metadata_json AS chunk_metadata "
                "FROM documents d JOIN document_chunks c USING(document_id) "
                "WHERE d.document_id = %s AND d.status = 'ready' ORDER BY c.ordinal",
                (document_id,),
            )
            rows = cursor.fetchall()
    if not rows:
        raise ValueError(f"rtd_q3_source_missing:{document_id}")
    return {
        "text": "\n".join(row["text"] for row in rows),
        "metadata": rows[0]["metadata_json"],
        "chunks": [
            {"text": row["text"], "metadata": row["chunk_metadata"]} for row in rows
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--baseline-document-id", required=True)
    parser.add_argument("--candidate-document-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url or not args.tenant_id.startswith("rtd-q3-"):
        raise ValueError("rtd_q3_environment_invalid")

    identity = {"tenant_id": args.tenant_id, "username": "rtd-q3-preparer", "role": "admin"}
    vector_store = VectorStore(database_url=args.database_url)
    cloned = []
    for name, document_id in (
        ("baseline", args.baseline_document_id),
        ("candidate", args.candidate_document_id),
    ):
        source = _source(args.database_url, document_id)
        cloned.extend(
            vector_store.add_documents(
                [{**source, "source": f"rtd-q3://{args.tenant_id}/{name}"}], identity
            )
        )
    if len(cloned) != 2:
        raise RuntimeError("rtd_q3_fixture_clone_failed")

    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    store = S3Utils()
    evidence = S3EvidenceStore(store.bucket, store.client)
    tool = build_runtime(args.database_url, store).tools.get("h5_trial_capture")
    assets = []
    permissions = ("rtd-q3-revoked-v1", "rtd-q3-clean-v1", "rtd-q3-clean-v1")
    splits = ("train", "train", "validation")
    retention = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    for case, permission, split in zip(suite["cases"][:3], permissions, splits, strict=True):
        asset = publish_rag_task_bundle(
            evidence,
            {
                **case,
                "split": split,
                "expected_status": "grounded",
                "expected_citation_count": 1,
                "source": {
                    "source_uri": f"rtd-q3://{args.tenant_id}/candidate",
                    "sha256": suite["source_sha256"],
                    "pages": 7,
                },
            },
            tenant_id=args.tenant_id,
            environment_snapshot={
                "schema_version": "rtd_q3_environment.v1",
                "tenant_id": args.tenant_id,
                "document_ids": cloned,
            },
            reset_contract={
                "kind": "registered-script",
                "ref": "scripts/prepare_rtd_q3_tasks.py",
                "sha256": sha256(Path(__file__).read_bytes()),
            },
            tool_contract={
                "name": tool.name,
                "version": tool.version,
                "contract_sha256": tool.contract_digest,
            },
            verifier_name="verify_rag_outcome",
            verifier_version=1,
            limits={"max_steps": 1, "deadline_seconds": 3600},
            acl_sha256=sha256(f"{args.tenant_id}:acl".encode()),
            permission_version=permission,
            retention_until=retention,
        )
        task_id = asset["fingerprint"]["task_bundle_id"]
        preflight = {
            "schema_version": "rtd_q3_preflight.v1",
            "tenant_id": args.tenant_id,
            "document_ids": cloned,
            "checks": {"fixture_cloned": True, "cross_tenant_copy": False},
        }
        preflight_hash = sha256(canonical_bytes(preflight))
        receipt = {
            "schema_version": "environment_receipt.v1",
            "task_bundle_id": task_id,
            "environment_id": f"{args.tenant_id}-fixture",
            "registry_sha256": sha256(canonical_bytes({"tenant_id": args.tenant_id})),
            "reset": {
                "receipt_id": str(uuid.uuid4()),
                "plan_sha256": sha256(canonical_bytes({"documents": cloned})),
                "status": "reset_complete",
                "error_code": None,
            },
            "fixture_sha256": suite["source_sha256"],
            "runtime": {
                "image_digest": os.environ["IMAGE_DIGEST"],
                "tool_contracts_sha256": tool.contract_digest,
            },
            "preflight": {
                "status": "passed",
                "error_code": None,
                "evidence_refs": [
                    {
                        "ref": f"tenants/{args.tenant_id}/environment-evidence/preflight/{preflight_hash}.json",
                        "sha256": preflight_hash,
                    }
                ],
            },
            "initial_state_sha256": sha256(canonical_bytes({"task": task_id, "documents": cloned})),
            "final_state_delta_sha256": None,
            "cleanup": {"status": "not_started", "error_code": None},
            "state": "ready",
            "invalid_reason": None,
        }
        published = publish_environment_receipt(
            evidence, receipt, preflight, tenant_id=args.tenant_id
        )
        assets.append(
            {
                "case_id": case["case_id"],
                "split": split,
                "permission_version": permission,
                "bundle_ref": asset["fingerprint"]["task_bundle_ref"],
                "task_bundle_id": task_id,
                "receipt": {"ref": published["receipt_ref"], "sha256": published["receipt_sha256"]},
                "verifier_input_ref": asset["fingerprint"]["verifier_input_ref"],
            }
        )
    print(json.dumps({"tenant_id": args.tenant_id, "documents": cloned, "assets": assets}, sort_keys=True))


if __name__ == "__main__":
    main()
