"""Run a document-scoped old/new RAG projection comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATABASE_URL, get_model_config
from core.evidence import S3EvidenceStore, canonical_bytes, fingerprint, sha256
from harness.experience import _put_immutable
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from utils.s3_utils import S3Utils

SUITE_PATH = Path(__file__).with_name("fixtures") / "rag_projection_ab_suite.json"


def score_results(results: list[dict[str, Any]], case: dict[str, Any]) -> dict[str, Any]:
    """Score retrieval using the frozen expected locator and content markers."""
    required_pages = set(case["required_pages"])
    expected_page_results = [
        item
        for item in results
        if item.get("metadata", {}).get("locator", {}).get("page") in required_pages
    ]
    rank = next(
        (
            index
            for index, item in enumerate(results, start=1)
            if item.get("metadata", {}).get("locator", {}).get("page") in required_pages
        ),
        None,
    )
    context = "\n".join(item["text"] for item in expected_page_results)
    return {
        "recall": float(rank is not None),
        "reciprocal_rank": 0.0 if rank is None else 1.0 / rank,
        "context_coverage": float(all(term in context for term in case["required_substrings"])),
        "citation_precision": (len(expected_page_results) / len(results) if results else 0.0),
        "returned_pages": [
            item.get("metadata", {}).get("locator", {}).get("page") for item in results
        ],
    }


def aggregate(cases: list[dict[str, Any]]) -> dict[str, float]:
    count = len(cases)
    return {
        name: round(sum(item[name] for item in cases) / count, 6)
        for name in ("recall", "reciprocal_rank", "context_coverage", "citation_precision")
    }


def _document(
    vector_store: VectorStore, identity: dict[str, str], document_id: str
) -> dict[str, Any]:
    with vector_store.database.transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.source_uri, c.ordinal, c.text, c.metadata_json "
                "FROM documents d JOIN document_chunks c USING (document_id) "
                "WHERE d.document_id = %s AND d.status = 'ready' ORDER BY c.ordinal",
                (document_id,),
            )
            rows = cursor.fetchall()
    if not rows:
        raise ValueError(f"rag_ab_document_missing:{document_id}")
    versions = {row["metadata_json"].get("source_version") for row in rows}
    if len(versions) != 1:
        raise ValueError(f"rag_ab_source_version_invalid:{document_id}")
    return {
        "document_id": document_id,
        "source_uri": rows[0]["source_uri"],
        "source_version": versions.pop(),
        "chunks": [
            {"ordinal": row["ordinal"], "text": row["text"], "metadata": row["metadata_json"]}
            for row in rows
        ],
    }


def _page_texts(document: dict[str, Any], *, parent: bool) -> dict[int, str]:
    pages: dict[int, str] = {}
    for chunk in document["chunks"]:
        metadata = chunk["metadata"]
        page = metadata.get("locator", {}).get("page")
        text = metadata.get("parent_context") if parent else chunk["text"]
        if type(page) is int and isinstance(text, str):
            existing = pages.setdefault(page, text)
            if existing != text:
                raise ValueError(f"rag_ab_page_content_ambiguous:{page}")
    return pages


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in root.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"rag_ab_model_files_missing:{root}")
    for item in files:
        digest.update(item.relative_to(root).as_posix().encode())
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _model_evidence() -> dict[str, Any]:
    config = get_model_config("model_b")
    values = {}
    for kind, path_key, id_key in (
        ("embedding", "model_path", "model_id"),
        ("reranker", "reranker_path", "reranker_id"),
    ):
        path = Path(config[path_key]).resolve()
        if not path.is_dir():
            raise ValueError(f"rag_ab_{kind}_path_missing:{path}")
        values[kind] = {
            "model_id": config[id_key],
            "path": str(path),
            "tree_sha256": _tree_digest(path),
        }
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-document-id", required=True)
    parser.add_argument("--candidate-document-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--username", default="rtd1-rag-ab")
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    for value in (args.baseline_document_id, args.candidate_document_id):
        uuid.UUID(value)
    if args.top_k < 1:
        raise ValueError("rag_ab_top_k_invalid")

    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    suite_sha256 = sha256(canonical_bytes(suite))
    identity = {"tenant_id": args.tenant_id, "username": args.username, "role": "admin"}
    vector_store = VectorStore(database_url=args.database_url)
    retriever = Retriever(vector_store)
    documents = {
        "baseline": _document(vector_store, identity, args.baseline_document_id),
        "candidate": _document(vector_store, identity, args.candidate_document_id),
    }
    expected_version = f"sha256:{suite['source_sha256']}"
    if {item["source_version"] for item in documents.values()} != {expected_version}:
        raise ValueError("rag_ab_source_version_mismatch")
    if _page_texts(documents["baseline"], parent=False) != _page_texts(
        documents["candidate"], parent=True
    ):
        raise ValueError("rag_ab_canonical_content_changed")
    candidate_chunks = documents["candidate"]["chunks"]
    if not candidate_chunks or any(
        chunk["metadata"].get("chunk_policy_version") != "rag-structure-v1"
        or not chunk["metadata"].get("source_span_ids")
        for chunk in candidate_chunks
    ):
        raise ValueError("rag_ab_candidate_lineage_missing")

    # Load both models before timing either arm.
    retriever.retrieve(
        suite["cases"][0]["query"], identity, top_k=1, document_ids=[args.baseline_document_id]
    )
    outcomes = {"baseline": [], "candidate": []}
    latencies = {"baseline": [], "candidate": []}
    for index, case in enumerate(suite["cases"]):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for arm in order:
            started = time.perf_counter()
            results = retriever.retrieve(
                case["query"],
                identity,
                top_k=args.top_k,
                document_ids=[documents[arm]["document_id"]],
            )
            latencies[arm].append((time.perf_counter() - started) * 1000)
            outcomes[arm].append({"case_id": case["case_id"], **score_results(results, case)})

    metrics = {arm: aggregate(outcomes[arm]) for arm in outcomes}
    for arm in metrics:
        metrics[arm]["mean_latency_ms"] = round(sum(latencies[arm]) / len(latencies[arm]), 3)
    quality_metrics = ("recall", "reciprocal_rank", "context_coverage", "citation_precision")
    passed = all(
        metrics["candidate"][name] >= metrics["baseline"][name] for name in quality_metrics
    )
    report = {
        "schema_version": "rag_projection_ab.v1",
        "decision": "PASS" if passed else "FAIL",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": args.tenant_id,
        "suite": {"id": suite["suite_id"], "sha256": suite_sha256, "cases": len(suite["cases"])},
        "source_version": expected_version,
        "top_k": args.top_k,
        "runtime": {**fingerprint(), "models": _model_evidence()},
        "arms": {
            arm: {
                "document_id": document["document_id"],
                "source_uri": document["source_uri"],
                "chunks": len(document["chunks"]),
                "metrics": metrics[arm],
                "cases": outcomes[arm],
            }
            for arm, document in documents.items()
        },
        "gate": {
            "quality_metrics": list(quality_metrics),
            "rule": "candidate >= baseline for every quality metric",
            "canonical_content_equal": True,
            "candidate_lineage_complete": True,
        },
    }
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{args.tenant_id}/evaluations/rag-projection-ab/sha256/{digest}.json"
    s3 = S3Utils()
    _put_immutable(S3EvidenceStore(s3.bucket, s3.client), ref, body)
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "report_ref": ref,
                "report_sha256": digest,
                "metrics": metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
