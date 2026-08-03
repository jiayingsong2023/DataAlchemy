"""Build reviewed SFT candidates from a verified normalized PDF corpus.

The script is intentionally offline: it never approves data, writes a snapshot,
or starts a LoRA Job.  The input QA JSONL must already contain reviewer-owned
labels and explicit training permission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid_jsonl:{path}:{line_no}") from error
        if not isinstance(value, dict):
            raise ValueError(f"jsonl_record_invalid:{path}:{line_no}")
        rows.append(value)
    return rows


def build_candidates(  # noqa: C901 - one guarded validator is easier to audit than a helper graph
    corpus: dict[str, Any], reviewed_qa: list[dict[str, Any]], tenant_id: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(corpus, dict) or not corpus.get("tenant_id") or not corpus.get("documents"):
        raise ValueError("normalized_corpus_invalid")
    corpus_tenant = corpus["tenant_id"]
    if tenant_id and tenant_id != corpus_tenant:
        raise ValueError("tenant_mismatch")
    chunks: dict[str, dict[str, Any]] = {}
    for document in corpus["documents"]:
        if document.get("tenant_id", corpus_tenant) != corpus_tenant:
            raise ValueError("source_tenant_mismatch")
        if not document.get("acl_digest") or document.get("trust_label") != "untrusted_external":
            raise ValueError("source_lineage_missing")
        for chunk in document.get("chunks", []):
            chunk_id = chunk.get("chunk_id")
            text = str(chunk.get("text", "")).strip()
            if not chunk_id or not text or chunk_id in chunks:
                raise ValueError("source_chunk_invalid")
            chunks[chunk_id] = {
                "text": text,
                "source_uri": document.get("source_uri"),
                "source_version": document.get("source_version"),
                "acl_digest": document["acl_digest"],
                "page": chunk.get("page"),
            }
    candidates = []
    seen_sources: set[str] = set()
    for item in reviewed_qa:
        source_id = item.get("source_chunk_id")
        if source_id not in chunks or source_id in seen_sources:
            raise ValueError("source_chunk_missing_or_duplicate")
        if item.get("review_status") != "approved" or item.get("training_allowed") is not True:
            raise ValueError("training_permission_missing")
        if item.get("split") not in {"train", "validation"}:
            raise ValueError("split_invalid")
        if not all(str(item.get(key, "")).strip() for key in ("instruction", "output")):
            raise ValueError("training_text_missing")
        source = chunks[source_id]
        source_sha256 = item.get("source_sha256") or digest(source["text"])
        if len(source_sha256) != 64:
            raise ValueError("source_hash_invalid")
        candidates.append({
            "split": item["split"],
            "review_status": "approved",
            "training_allowed": True,
            "instruction": item["instruction"],
            "input": item.get("input", ""),
            "output": item["output"],
            "provenance": {
                "source_chunk_id": source_id,
                "source_sha256": source_sha256,
                "source_uri": source["source_uri"],
                "source_version": source["source_version"],
                "page": source["page"],
                "source_acl_digest": source["acl_digest"],
                "tenant_id": corpus_tenant,
                "training_purpose": item.get("training_purpose", "pdf_qa_improvement"),
                "training_permission_version": item.get("permission_version", "h6-pdf-v1"),
            },
        })
        seen_sources.add(source_id)
    if not candidates:
        raise ValueError("training_candidates_empty")
    train_count = sum(item.get("split") == "train" for item in reviewed_qa)
    validation_count = sum(item.get("split") == "validation" for item in reviewed_qa)
    if not train_count or not validation_count:
        raise ValueError("train_validation_split_missing")
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in candidates).encode()
    return candidates, {
        "tenant_id": corpus_tenant,
        "source_corpus_sha256": digest(corpus),
        "dataset_sha256": hashlib.sha256(body).hexdigest(),
        "records": len(candidates),
        "train": train_count,
        "validation": validation_count,
        "reviewed_input_required": True,
        "source_chunks": sorted(seen_sources),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="verified normalized corpus JSON")
    parser.add_argument("--reviewed-qa", type=Path, required=True, help="reviewed QA JSONL")
    parser.add_argument("--output", type=Path, required=True, help="candidate SFT JSONL")
    parser.add_argument("--manifest", type=Path, required=True, help="candidate manifest JSON")
    parser.add_argument("--tenant-id")
    args = parser.parse_args()
    candidates, manifest = build_candidates(
        json.loads(args.corpus.read_text(encoding="utf-8")), read_jsonl(args.reviewed_qa), args.tenant_id
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in candidates), encoding="utf-8")
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
