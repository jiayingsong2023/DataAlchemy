"""Qualify the governed chat path through the deployed HTTP ingress."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from harness.experience import _put_immutable
from harness.rtd_q4_performance import _load, _score, _summary
from utils.auth import create_access_token
from utils.s3_utils import S3Utils

_SUITE = Path(__file__).with_name("fixtures") / "rag_projection_ab_suite.json"


def _post(url: str, host: str, token: str, query: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/chat",
        data=json.dumps({"query": query}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Host": host,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"http_{error.code}:{error.read().decode(errors='replace')}") from error


async def _request(
    case: dict[str, Any], tenant_id: str, url: str, host: str
) -> dict[str, Any]:
    started = time.perf_counter()
    username = f"rtd-q4-http-{uuid.uuid4()}"
    token = create_access_token({"sub": username, "tenant_id": tenant_id, "role": "admin"})
    try:
        response = await asyncio.to_thread(_post, url, host, token, case["query"])
        error = None
    except Exception as exc:
        response = {"answer": "", "citations": []}
        error = f"{type(exc).__name__}:{exc}"
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "case_id": case["case_id"],
        "passed": error is None
        and _score(case, response["answer"], response.get("citations", [])),
        "error": error,
        "end_to_end_ms": round(elapsed_ms, 3),
        "run_id": response.get("run_id"),
        "answer_sha256": sha256(response["answer"].encode()),
        "citation_chunk_ids": sorted(
            str(item["chunk_id"])
            for item in response.get("citations", [])
            if item.get("chunk_id")
        ),
        "model_execution": response.get("model_execution"),
    }


async def _level(
    concurrency: int,
    repetitions: int,
    cases: list[dict[str, Any]],
    tenant_id: str,
    url: str,
    host: str,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _request(case, tenant_id, url, host)

    started = time.perf_counter()
    rows = await asyncio.gather(
        *(limited(case) for _ in range(repetitions) for case in cases)
    )
    elapsed = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "requests": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "errors": sum(row["error"] is not None for row in rows),
        "elapsed_ms": round(elapsed * 1000, 3),
        "throughput_rps": round(len(rows) / elapsed, 6),
        "end_to_end_ms": _summary([row["end_to_end_ms"] for row in rows]),
        "cases": rows,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    build_git_sha = os.getenv("BUILD_GIT_SHA")
    image_digest = os.getenv("IMAGE_DIGEST")
    if not build_git_sha or not image_digest or os.getenv("EXECUTION_MODE") != "local":
        raise RuntimeError("rtd_q4_http_runtime_fingerprint_missing")
    direct = _load(args.evidence_bucket, args.direct_ref, args.direct_sha256)
    if (
        direct.get("decision") != "PASS"
        or direct.get("runtime", {}).get("build_git_sha") != args.target_build_git_sha
        or direct.get("runtime", {}).get("image_digest") != args.target_image_digest
    ):
        raise RuntimeError("rtd_q4_http_direct_prerequisite_mismatch")
    suite = json.loads(_SUITE.read_text(encoding="utf-8"))
    await _request(suite["cases"][0], args.tenant_id, args.url, args.host)
    levels = [
        await _level(
            concurrency,
            args.repetitions,
            suite["cases"],
            args.tenant_id,
            args.url,
            args.host,
        )
        for concurrency in args.concurrency
    ]
    gates = [
        {
            "concurrency": level["concurrency"],
            "quality_passed": level["passed"] == level["requests"],
            "errors_passed": level["errors"] == 0,
            "p95_passed": level["end_to_end_ms"]["p95"] <= 30000,
            "p99_passed": level["end_to_end_ms"]["p99"] <= 45000,
            "throughput_passed": level["throughput_rps"] >= 0.03,
        }
        for level in levels
    ]
    passed = all(
        all(value for key, value in gate.items() if key.endswith("_passed")) for gate in gates
    )
    return {
        "schema_version": "rtd_q4_http_ingress.v1",
        "decision": "PASS" if passed else "NO-GO",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": args.tenant_id,
        "runtime": {
            "driver_build_git_sha": build_git_sha,
            "driver_image_digest": image_digest,
            "target_build_git_sha": args.target_build_git_sha,
            "target_image_digest": args.target_image_digest,
            "url": args.url,
            "host": args.host,
            "cache_policy": "unique_identity_per_request",
        },
        "direct_prerequisite": {"ref": args.direct_ref, "sha256": args.direct_sha256},
        "plan": {
            "concurrency": args.concurrency,
            "repetitions": args.repetitions,
            "cases": len(suite["cases"]),
            "requests_per_level": len(suite["cases"]) * args.repetitions,
        },
        "levels": levels,
        "gates": gates,
        "limitations": ["public_synthetic_engineering_only", "local_single_node_k3d"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--evidence-bucket", default="data-alchemy")
    parser.add_argument("--direct-ref", required=True)
    parser.add_argument("--direct-sha256", required=True)
    parser.add_argument("--target-build-git-sha", required=True)
    parser.add_argument("--target-image-digest", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    body = canonical_bytes(report)
    digest = sha256(body)
    ref = f"tenants/{args.tenant_id}/qualification/rtd-q4/http/sha256/{digest}.json"
    s3 = S3Utils(args.evidence_bucket)
    _put_immutable(S3EvidenceStore(s3.bucket, s3.client), ref, body)
    print(json.dumps({"decision": report["decision"], "ref": ref, "sha256": digest}))
    if report["decision"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
