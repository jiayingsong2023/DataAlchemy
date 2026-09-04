"""Run the RTD4 base+RAG versus promoted-adapter+RAG engineering gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from config import DATABASE_URL, VERIFIER_DATABASE_URL
from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.experience import _put_immutable
from harness.release_policy import validate_release_decision
from inference.adapter_runtime import AdapterRuntime
from rag.answering import GroundedAnswering, answer_with_citations
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from utils.s3_utils import S3Utils

SUITE = Path(__file__).with_name("fixtures") / "rag_projection_ab_suite.json"


def _percentile(values: list[float], percentile: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[int(percentile) - 1]


def _object(bucket: str, ref: str, expected: str) -> dict[str, Any]:
    body = S3Utils(bucket).get_object_body(ref)
    if body is None or sha256(body) != expected:
        raise ValueError(f"rtd4_evidence_hash_mismatch:{bucket}:{ref}")
    return json.loads(body)


def _artifact_evidence(bucket: str, prefix: str) -> dict[str, Any]:
    s3 = S3Utils(bucket)
    objects = sorted(s3.list_objects(prefix), key=lambda item: item["Key"])
    if not objects:
        raise ValueError(f"rtd4_adapter_artifact_missing:{bucket}:{prefix}")
    digest = hashlib.sha256()
    size = 0
    for item in objects:
        key = item["Key"]
        body = s3.get_object_body(key)
        if body is None:
            raise ValueError(f"rtd4_adapter_artifact_read_failed:{bucket}:{key}")
        relative = key.removeprefix(prefix).lstrip("/")
        digest.update(relative.encode())
        digest.update(body)
        size += len(body)
    return {"bucket": bucket, "prefix": prefix, "sha256": digest.hexdigest(), "size": size}


def _evaluation_release_passed(
    services: ReadOnlyServices, evaluation_id: str, release_id: str, adapter_id: str
) -> bool:
    evaluation = services.evaluation(evaluation_id)
    release = services.release(release_id)
    adapter = services.adapter(adapter_id)
    return bool(
        evaluation
        and evaluation["state"] == "passed"
        and evaluation["subject_type"] == "adapter"
        and str(evaluation["subject_ref"]) == adapter_id
        and release
        and release["status"] == "promoted"
        and str(release["adapter_id"]) == adapter_id
        and str(release["evaluation_id"]) == evaluation_id
        and adapter
        and adapter["state"] == "verified"
    )


def _score(case: dict[str, Any], answer: str, citations: list[dict[str, Any]]) -> dict[str, Any]:
    pages = {
        citation.get("locator", {}).get("page")
        for citation in citations
        if isinstance(citation.get("locator"), dict)
    }
    return {
        "required_text_passed": all(value in answer for value in case["required_substrings"]),
        "required_page_passed": bool(pages & set(case["required_pages"])),
        "citation_lineage_passed": bool(citations)
        and all(
            citation.get("source_span_ids")
            and citation.get("source_content_sha256")
            and citation.get("acl_digest")
            for citation in citations
        ),
        "returned_pages": sorted(page for page in pages if isinstance(page, int)),
    }


async def _run_arm(
    name: str,
    identity: dict[str, str],
    cases: list[dict[str, Any]],
    contexts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    use_adapter = name == "adapter_rag"
    os.environ["H5_LORA_MODE"] = "single_tenant_lora" if use_adapter else "disabled"
    os.environ["MODEL_RELEASE_TENANT_ID"] = identity["tenant_id"] if use_adapter else ""
    runtime = AdapterRuntime(adapter_path=f"/tmp/rtd4-{name}-adapter")
    answering = GroundedAnswering()
    outcomes = []
    cache_scope = f"rtd4:{uuid.uuid4()}:{name}"
    started = time.perf_counter()
    try:
        for case in cases:
            calls: list[dict[str, Any]] = []
            case_started = time.perf_counter()
            answer, citations, status = await answer_with_citations(
                case["query"],
                identity,
                contexts[case["case_id"]],
                runtime,
                answering,
                cache_scope=cache_scope,
                trace_recorder=calls.append,
            )
            score = _score(case, answer, citations)
            outcomes.append(
                {
                    "case_id": case["case_id"],
                    "passed": all(value for key, value in score.items() if key.endswith("_passed")),
                    "answer_sha256": sha256(answer.encode()),
                    "citation_chunk_ids": sorted(
                        str(item["chunk_id"]) for item in citations if item.get("chunk_id")
                    ),
                    "model_response_sha256": sha256(str(calls[-1].get("response", "")).encode())
                    if calls
                    else None,
                    "model_execution": status,
                    "latency_ms": round((time.perf_counter() - case_started) * 1000, 3),
                    **score,
                }
            )
        model_status = runtime.model_status(identity)
    finally:
        runtime.model_manager.unload_models()
    total_latency_ms = (time.perf_counter() - started) * 1000
    latencies = [item["latency_ms"] for item in outcomes]
    return {
        "name": name,
        "cases": outcomes,
        "passed": all(item["passed"] for item in outcomes),
        "latency_ms": round(total_latency_ms, 3),
        "performance": {
            "p95_latency_ms": round(_percentile(latencies, 95), 3),
            "p99_latency_ms": round(_percentile(latencies, 99), 3),
            "throughput_rps": round(len(outcomes) / (total_latency_ms / 1000), 6),
        },
        "model_execution": model_status,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if not DATABASE_URL or not VERIFIER_DATABASE_URL or os.getenv("EXECUTION_MODE") != "local":
        raise RuntimeError("rtd4_local_environment_invalid")
    if not os.getenv("BUILD_GIT_SHA") or not os.getenv("IMAGE_DIGEST"):
        raise RuntimeError("rtd4_runtime_fingerprint_missing")
    if not re.fullmatch(r"[0-9a-f]{40}", args.rollback_commit):
        raise ValueError("rtd4_rollback_commit_invalid")
    identity = {"tenant_id": args.tenant_id, "username": "rtd4-runner", "role": "admin"}
    rag_report = _object(args.evidence_bucket, args.rag_report_ref, args.rag_report_sha256)
    revocation = _object(
        args.evidence_bucket, args.revocation_receipt_ref, args.revocation_receipt_sha256
    )
    services = ReadOnlyServices(VERIFIER_DATABASE_URL, identity)
    decision = None
    evaluation_id = getattr(args, "release_evaluation_id", None)
    if bool(args.release_decision_ref) != bool(args.release_decision_sha256):
        raise ValueError("rtd4_release_decision_descriptor_invalid")
    if bool(evaluation_id) == bool(args.release_decision_ref):
        raise ValueError("rtd4_release_evidence_ambiguous")
    if evaluation_id:
        release_evidence_passed = _evaluation_release_passed(
            services, evaluation_id, args.expected_release_id, args.expected_adapter_id
        )
    else:
        decision = validate_release_decision(
            _object(args.release_bucket, args.release_decision_ref, args.release_decision_sha256)
        )
        verified = (
            default_verifiers()
            .get("verify_release_decision", 1)
            .handler(
                {
                    "parameters": {
                        "decision_ref": args.release_decision_ref,
                        "decision_sha256": args.release_decision_sha256,
                    }
                },
                identity,
                {},
                services,
            )
        )
        release_evidence_passed = (
            decision["result"].get("status") == "GO" and verified.status == "passed"
        )
    if (
        rag_report.get("decision") != "PASS"
        or revocation.get("decision") != "PASS"
        or not release_evidence_passed
    ):
        raise RuntimeError("rtd4_prior_gate_failed")
    artifacts = [
        _artifact_evidence(bucket, args.expected_artifact_key)
        for bucket in (args.release_bucket, args.runtime_artifact_bucket)
    ]
    if any(
        item["sha256"] != args.expected_artifact_sha256
        or item["size"] != args.expected_artifact_size
        for item in artifacts
    ):
        raise RuntimeError("rtd4_adapter_artifact_hash_mismatch")

    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    if rag_report["suite"]["sha256"] != sha256(canonical_bytes(suite)):
        raise ValueError("rtd4_rag_suite_mismatch")
    document_id = rag_report["arms"]["candidate"]["document_id"]
    source_version = rag_report["source_version"]
    retriever = Retriever(VectorStore(database_url=DATABASE_URL))
    contexts = {
        case["case_id"]: [
            {**item, "context_type": "document"}
            for item in retriever.retrieve(
                case["query"],
                identity,
                top_k=5,
                source_version=source_version,
                document_ids=[document_id],
            )
        ]
        for case in suite["cases"]
    }
    base, adapter = (
        await _run_arm("base_rag", identity, suite["cases"], contexts),
        await _run_arm("adapter_rag", identity, suite["cases"], contexts),
    )
    paired = [
        {
            "case_id": left["case_id"],
            "answer_equal": left["answer_sha256"] == right["answer_sha256"],
            "citations_equal": left["citation_chunk_ids"] == right["citation_chunk_ids"],
        }
        for left, right in zip(base["cases"], adapter["cases"], strict=True)
    ]
    active = adapter["model_execution"]
    passed = (
        base["passed"]
        and adapter["passed"]
        and all(item["answer_equal"] and item["citations_equal"] for item in paired)
        and active.get("adapter_id") == args.expected_adapter_id
        and active.get("adapter_artifact_sha256") == args.expected_artifact_sha256
        and active.get("release_id") == args.expected_release_id
    )
    return {
        "schema_version": "rtd4_joint_gate.v1",
        "decision": "PASS" if passed else "NO-GO",
        "tenant_id": args.tenant_id,
        "runtime": {
            "build_git_sha": os.environ["BUILD_GIT_SHA"],
            "image_digest": os.environ["IMAGE_DIGEST"],
            "execution_mode": "local",
            "answer_policy": "rag_authoritative_adapter_intuition_non_authoritative",
        },
        "prior_gates": {
            "rag_projection": {"ref": args.rag_report_ref, "sha256": args.rag_report_sha256},
            "revocation": {
                "ref": args.revocation_receipt_ref,
                "sha256": args.revocation_receipt_sha256,
            },
            "adapter_release": (
                {
                    "evaluation_id": evaluation_id,
                    "release_id": args.expected_release_id,
                    "adapter_id": args.expected_adapter_id,
                    "independently_replayed": True,
                }
                if evaluation_id
                else {
                    "bucket": args.release_bucket,
                    "ref": args.release_decision_ref,
                    "sha256": args.release_decision_sha256,
                    "independently_replayed": True,
                    "base_pass_rate": decision["result"]["base_normal_pass_rate"],
                    "candidate_pass_rate": decision["result"]["candidate_normal_pass_rate"],
                    "candidate_fingerprint_sha256": decision["candidate_fingerprint_sha256"],
                }
            ),
        },
        "adapter_artifacts": artifacts,
        "cleanup": {
            "deleted": [
                "scripts/build_pdf_training_candidates.py",
                "tests/test_pdf_training_candidates.py",
            ],
            "ci_ratchet": "deleted_pdf_candidate_builder_must_remain_absent",
            "rollback_commit": args.rollback_commit,
        },
        "rag_suite": rag_report["suite"],
        "document_id": document_id,
        "arms": [base, adapter],
        "paired": paired,
        "joint_effect": "neutral_by_local_rag_authority_policy",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--evidence-bucket", default="data-alchemy")
    parser.add_argument("--release-bucket", default=os.getenv("S3_BUCKET", "data-alchemy-test"))
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-adapter-id", required=True)
    parser.add_argument("--expected-artifact-key", required=True)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument("--expected-artifact-size", required=True, type=int)
    parser.add_argument("--runtime-artifact-bucket", default="data-alchemy")
    parser.add_argument("--rollback-commit", required=True)
    for name in ("rag-report", "revocation-receipt"):
        parser.add_argument(f"--{name}-ref", required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--release-decision-ref")
    parser.add_argument("--release-decision-sha256")
    parser.add_argument("--release-evaluation-id")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(run(args))
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{args.tenant_id}/evaluations/rtd4/sha256/{digest}.json"
    _put_immutable(
        S3EvidenceStore(args.evidence_bucket, S3Utils(args.evidence_bucket).client), ref, body
    )
    print(
        json.dumps(
            {"decision": report["decision"], "receipt_ref": ref, "receipt_sha256": digest},
            sort_keys=True,
        )
    )
    if report["decision"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
