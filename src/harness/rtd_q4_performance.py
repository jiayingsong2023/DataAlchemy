"""Run the RTD-Q4 corpus-scale, concurrent RAG projection A/B."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import resource
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import torch

from config import DATABASE_URL
from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from harness.experience import _put_immutable
from harness.qualification import validate_qualification_manifest
from inference.adapter_runtime import AdapterRuntime
from rag.answering import GroundedAnswering, answer_with_citations
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from utils.s3_utils import S3Utils

_STAGES = (
    "embedding_ms",
    "vector_ms",
    "fts_ms",
    "fusion_ms",
    "reranker_ms",
    "retrieval_ms",
    "generation_ms",
    "end_to_end_ms",
)
_SUITE = Path(__file__).with_name("fixtures") / "rag_projection_ab_suite.json"


def _load(bucket: str, ref: str, expected_sha256: str) -> dict[str, Any]:
    body = S3Utils(bucket).get_object_body(ref)
    if body is None or sha256(body) != expected_sha256:
        raise ValueError(f"rtd_q4_evidence_hash_mismatch:{ref}")
    return json.loads(body)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(sum(values) / len(values), 3),
        "p50": round(_percentile(values, 50), 3),
        "p95": round(_percentile(values, 95), 3),
        "p99": round(_percentile(values, 99), 3),
    }


def _score(case: dict[str, Any], answer: str, citations: list[dict[str, Any]]) -> bool:
    pages = {
        item.get("locator", {}).get("page")
        for item in citations
        if isinstance(item.get("locator"), dict)
    }
    return bool(
        all(value in answer for value in case["required_substrings"])
        and pages.intersection(case["required_pages"])
        and citations
        and all(
            item.get("source_span_ids")
            and item.get("source_content_sha256")
            and item.get("acl_digest")
            for item in citations
        )
    )


def _inventory(
    store: VectorStore,
    identity: dict[str, str],
    baseline_document_id: str,
    candidate_document_id: str,
) -> dict[str, Any]:
    with store.database.transaction(identity, read_only=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT d.document_id, d.content_hash, d.version, count(c.chunk_id) AS chunks "
                "FROM documents d JOIN document_chunks c USING (document_id) "
                "WHERE d.status = 'ready' GROUP BY d.document_id, d.content_hash, d.version "
                "ORDER BY d.document_id"
            )
            rows = cursor.fetchall()
    documents = [
        {
            "document_id": str(row["document_id"]),
            "content_hash": row["content_hash"],
            "version": row["version"],
            "chunks": row["chunks"],
        }
        for row in rows
    ]
    by_id = {item["document_id"]: item for item in documents}
    if baseline_document_id not in by_id or candidate_document_id not in by_id:
        raise ValueError("rtd_q4_projection_document_missing")
    background = [
        item["document_id"]
        for item in documents
        if item["document_id"] not in {baseline_document_id, candidate_document_id}
    ]
    return {
        "documents": len(documents),
        "chunks": sum(item["chunks"] for item in documents),
        "sha256": sha256(canonical_bytes(documents)),
        "arms": {
            "stable": {
                "projection_document_id": baseline_document_id,
                "document_ids": [*background, baseline_document_id],
                "chunks": sum(by_id[item]["chunks"] for item in background)
                + by_id[baseline_document_id]["chunks"],
            },
            "candidate": {
                "projection_document_id": candidate_document_id,
                "document_ids": [*background, candidate_document_id],
                "chunks": sum(by_id[item]["chunks"] for item in background)
                + by_id[candidate_document_id]["chunks"],
            },
        },
    }


async def _request(
    arm: str,
    case: dict[str, Any],
    document_ids: list[str],
    identity: dict[str, str],
    retriever: Retriever,
    runtime: AdapterRuntime,
    answering: GroundedAnswering,
) -> dict[str, Any]:
    timings: dict[str, float] = {}
    started = time.perf_counter()
    contexts = [
        {**item, "context_type": "document"}
        for item in retriever.retrieve(
            case["query"], identity, top_k=5, document_ids=document_ids, timings=timings
        )
    ]
    timings["retrieval_ms"] = (time.perf_counter() - started) * 1000
    generation_started = time.perf_counter()
    answer, citations, _ = await answer_with_citations(
        case["query"],
        identity,
        contexts,
        runtime,
        answering,
        cache_scope=f"rtd-q4:{uuid.uuid4()}",
    )
    timings["generation_ms"] = (time.perf_counter() - generation_started) * 1000
    timings["end_to_end_ms"] = (time.perf_counter() - started) * 1000
    return {
        "arm": arm,
        "case_id": case["case_id"],
        "passed": _score(case, answer, citations),
        "answer_sha256": sha256(answer.encode()),
        "citation_chunk_ids": sorted(
            str(item["chunk_id"]) for item in citations if item.get("chunk_id")
        ),
        "timings": {name: round(timings[name], 3) for name in _STAGES},
    }


async def _level(
    concurrency: int,
    repetitions: int,
    cases: list[dict[str, Any]],
    arms: dict[str, dict[str, Any]],
    identity: dict[str, str],
    retriever: Retriever,
    runtime: AdapterRuntime,
    answering: GroundedAnswering,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(arm: str, case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _request(
                arm,
                case,
                arms[arm]["document_ids"],
                identity,
                retriever,
                runtime,
                answering,
            )

    work = []
    for repetition in range(repetitions):
        for index, case in enumerate(cases):
            order = ("stable", "candidate") if (repetition + index) % 2 == 0 else (
                "candidate",
                "stable",
            )
            work.extend(limited(arm, case) for arm in order)
    started = time.perf_counter()
    rows = await asyncio.gather(*work)
    elapsed = time.perf_counter() - started
    results: dict[str, Any] = {}
    for arm in ("stable", "candidate"):
        selected = [row for row in rows if row["arm"] == arm]
        results[arm] = {
            "requests": len(selected),
            "passed": sum(row["passed"] for row in selected),
            "errors": 0,
            "throughput_rps": round(len(selected) / elapsed, 6),
            "stages": {
                name: _summary([row["timings"][name] for row in selected]) for name in _STAGES
            },
            "cases": selected,
        }
    return {
        "concurrency": concurrency,
        "elapsed_ms": round(elapsed * 1000, 3),
        "combined_throughput_rps": round(len(rows) / elapsed, 6),
        "arms": results,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.repetitions < 1 or not args.concurrency or any(value < 1 for value in args.concurrency):
        raise ValueError("rtd_q4_load_shape_invalid")
    build_git_sha = os.getenv("BUILD_GIT_SHA")
    image_digest = os.getenv("IMAGE_DIGEST")
    if not build_git_sha or not image_digest or os.getenv("EXECUTION_MODE") != "local":
        raise RuntimeError("rtd_q4_runtime_fingerprint_missing")
    qualification = validate_qualification_manifest(
        _load(args.evidence_bucket, args.qualification_ref, args.qualification_sha256)
    )
    rag = _load(args.evidence_bucket, args.rag_report_ref, args.rag_report_sha256)
    if qualification["state"] != "frozen" or rag.get("decision") != "PASS":
        raise RuntimeError("rtd_q4_prerequisite_failed")
    baseline = rag["arms"]["baseline"]["document_id"]
    candidate = rag["arms"]["candidate"]["document_id"]
    identity = {"tenant_id": args.tenant_id, "username": "rtd-q4-runner", "role": "admin"}
    vector_store = VectorStore(database_url=args.database_url)
    inventory = _inventory(vector_store, identity, baseline, candidate)
    retriever = Retriever(vector_store)
    runtime = AdapterRuntime(adapter_path="/tmp/rtd-q4-base-adapter")
    answering = GroundedAnswering()
    os.environ["H5_LORA_MODE"] = "disabled"
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    try:
        for arm in ("stable", "candidate"):
            await _request(
                arm,
                args.cases[0],
                inventory["arms"][arm]["document_ids"],
                identity,
                retriever,
                runtime,
                answering,
            )
        levels = [
            await _level(
                value,
                args.repetitions,
                args.cases,
                inventory["arms"],
                identity,
                retriever,
                runtime,
                answering,
            )
            for value in args.concurrency
        ]
        model_execution = runtime.model_status(identity)
    finally:
        runtime.model_manager.unload_models()
    slos = qualification["performance_slos"]
    gates = []
    for level in levels:
        stable, candidate_result = level["arms"]["stable"], level["arms"]["candidate"]
        stable_p95 = stable["stages"]["end_to_end_ms"]["p95"]
        candidate_p95 = candidate_result["stages"]["end_to_end_ms"]["p95"]
        ratio = candidate_p95 / stable_p95
        gates.append(
            {
                "concurrency": level["concurrency"],
                "quality_passed": stable["passed"] == stable["requests"]
                and candidate_result["passed"] == candidate_result["requests"],
                "p95_passed": candidate_p95 <= slos["p95_latency_ms"],
                "p99_passed": candidate_result["stages"]["end_to_end_ms"]["p99"]
                <= slos["p99_latency_ms"],
                "throughput_passed": candidate_result["throughput_rps"]
                >= slos["minimum_throughput_rps"],
                "p95_ratio": round(ratio, 6),
                "p95_ratio_passed": ratio <= slos["candidate_to_stable_p95_ratio"],
            }
        )
    passed = all(all(value for key, value in gate.items() if key.endswith("_passed")) for gate in gates)
    return {
        "schema_version": "rtd_q4_performance_ab.v1",
        "decision": "PASS" if passed else "NO-GO",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": args.tenant_id,
        "runtime": {
            "build_git_sha": build_git_sha,
            "image_digest": image_digest,
            "execution_mode": "local",
            "embedding_device": os.getenv("EMBEDDING_DEVICE", "cpu"),
            "reranker_device": os.getenv("RERANKER_DEVICE", "cpu"),
            "gpu_available": torch.cuda.is_available(),
        },
        "plan": {
            "qualification": {
                "ref": args.qualification_ref,
                "sha256": args.qualification_sha256,
            },
            "rag_projection": {"ref": args.rag_report_ref, "sha256": args.rag_report_sha256},
            "concurrency": args.concurrency,
            "repetitions": args.repetitions,
            "cases": len(args.cases),
            "requests_per_arm_per_level": len(args.cases) * args.repetitions,
            "cache_policy": "unique_scope_per_request",
            "arm_order": "deterministic_interleaved",
        },
        "inventory": inventory,
        "levels": levels,
        "gates": gates,
        "resources": {
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "gpu_peak_allocated_bytes": torch.cuda.max_memory_allocated()
            if torch.cuda.is_available()
            else 0,
            "gpu_peak_reserved_bytes": torch.cuda.max_memory_reserved()
            if torch.cuda.is_available()
            else 0,
        },
        "model_execution": model_execution,
        "limitations": [
            "public_synthetic_engineering_only",
            "local_single_node_k3d",
            "http_and_ingress_overhead_excluded",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--evidence-bucket", default="data-alchemy")
    parser.add_argument("--qualification-ref", required=True)
    parser.add_argument("--qualification-sha256", required=True)
    parser.add_argument("--rag-report-ref", required=True)
    parser.add_argument("--rag-report-sha256", required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--repetitions", type=int, default=3)
    return parser


def main() -> None:
    args = _parser().parse_args()
    rag = _load(args.evidence_bucket, args.rag_report_ref, args.rag_report_sha256)
    suite = json.loads(_SUITE.read_text(encoding="utf-8"))
    if sha256(canonical_bytes(suite)) != rag["suite"]["sha256"]:
        raise ValueError("rtd_q4_rag_suite_mismatch")
    args.cases = suite["cases"]
    report = asyncio.run(run(args))
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{args.tenant_id}/qualification/rtd-q4/performance/sha256/{digest}.json"
    s3 = S3Utils(args.evidence_bucket)
    _put_immutable(S3EvidenceStore(s3.bucket, s3.client), ref, body)
    print(json.dumps({"decision": report["decision"], "ref": ref, "sha256": digest}))
    if report["decision"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
