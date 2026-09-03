"""Deterministic contracts used by the H3 document-to-RAG pilot.

This module deliberately contains no planner or background worker.  It only
normalizes trusted server metadata, parses the two supported document formats,
and builds content-addressed records that the AgentRuntime can verify.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

from docx import Document
from pypdf import PdfReader

from etl.sanitizers import sanitize_text
from rag.chunkers.recursive import RecursiveChunker

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_ROUGH_BYTES = 50 * 1024 * 1024
MAX_ROUGH_RECORDS = 10_000
INJECTION_POLICY_VERSION = 1
PII_POLICY_VERSION = 1
PARSE_POLICY_VERSION = "canonical-parse-v1"
RAG_CHUNK_POLICY_VERSION = "rag-structure-v1"

_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.I),
    re.compile(r"(?:call|invoke|run|execute)\s+(?:the\s+)?(?:sync_git|tool|function)", re.I),
    re.compile(r"(?:save|write|store).{0,40}(?:long[- ]term\s+memory|memory)", re.I),
)


class DocumentRejected(ValueError):
    """A document failed a trust-boundary check."""

    def __init__(self, code: str, message: str = "document rejected"):
        super().__init__(code if message == "document rejected" else message)
        self.code = code


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def safe_filename(filename: str) -> str:
    name = unicodedata.normalize("NFKC", filename or "").replace("\\", "/")
    name = PurePosixPath(name).name.strip()
    if not name or name in {".", ".."}:
        raise DocumentRejected("filename_invalid")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if len(name) > 160:
        stem, dot, suffix = name.rpartition(".")
        name = (stem[: 160 - len(suffix) - (1 if dot else 0)] + dot + suffix) if dot else name[:160]
    return name


def _content_type(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    raise DocumentRejected("file_type_unsupported")


def validate_upload(filename: str, body: bytes, content_type: str | None = None) -> tuple[str, str]:
    """Validate one pilot upload and return ``(safe_name, content_type)``."""
    name = safe_filename(filename)
    kind = _content_type(name)
    if len(body) == 0:
        raise DocumentRejected("file_empty")
    if len(body) > MAX_DOCUMENT_BYTES:
        raise DocumentRejected("file_too_large")
    if content_type and content_type not in {kind, "application/octet-stream"}:
        raise DocumentRejected("mime_mismatch")
    if kind == "application/pdf" and not body.startswith(b"%PDF-"):
        raise DocumentRejected("pdf_signature_invalid")
    if kind.endswith("wordprocessingml.document") and not body.startswith(b"PK"):
        raise DocumentRejected("docx_signature_invalid")
    # Parsing at the boundary prevents an unparseable object from entering a run.
    records = parse_document(body, name)
    if not any(record["text"].strip() for record in records):
        raise DocumentRejected("document_text_empty")
    return name, kind


def _injection_codes(text: str) -> list[str]:
    return (
        ["prompt_injection_pattern"]
        if any(pattern.search(text) for pattern in _INJECTION_PATTERNS)
        else []
    )


def _normalized_text(text: str) -> str:
    text = sanitize_text(text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_document(body: bytes, filename: str) -> list[dict[str, Any]]:
    """Parse PDF pages or DOCX paragraphs without losing source locators."""
    kind = _content_type(filename)
    try:
        if kind == "application/pdf":
            reader = PdfReader(io.BytesIO(body))
            if reader.is_encrypted:
                raise DocumentRejected("pdf_encrypted")
            raw = [
                (index + 1, None, page.extract_text() or "")
                for index, page in enumerate(reader.pages)
            ]
        else:
            document = Document(io.BytesIO(body))
            raw = [
                (None, index, paragraph.text or "")
                for index, paragraph in enumerate(document.paragraphs)
            ]
    except DocumentRejected:
        raise
    except Exception as error:
        raise DocumentRejected("document_parse_failed", str(error)) from error

    records: list[dict[str, Any]] = []
    for page, paragraph, text in raw:
        normalized = _normalized_text(text)
        if not normalized:
            continue
        records.append(
            {
                "page": page,
                "paragraph": paragraph,
                "text": normalized,
                "injection_codes": _injection_codes(normalized),
            }
        )
    return records


def build_input_descriptor(
    *,
    input_id: str,
    tenant_id: str,
    source_uri: str,
    filename: str,
    content_type: str,
    body: bytes,
    acl: list[dict[str, str]],
    owner: str,
) -> dict[str, Any]:
    if not input_id or not tenant_id or not owner:
        raise ValueError("input identity is required")
    if not acl:
        raise DocumentRejected("acl_empty")
    acl_snapshot = [
        {
            "subject_type": item["subject_type"],
            "subject_id": item["subject_id"],
            "permission": "read",
        }
        for item in acl
    ]
    return {
        "schema_version": 1,
        "input_id": input_id,
        "tenant_id": tenant_id,
        "owner": owner,
        "source": {
            "type": "document",
            "uri": source_uri,
            "version": f"sha256:{sha256_bytes(body)}",
            "filename": filename,
            "content_type": content_type,
            "size": len(body),
        },
        "acl": sorted(acl_snapshot, key=lambda value: (value["subject_type"], value["subject_id"])),
        "acl_digest": digest(acl_snapshot),
        "trust_label": "untrusted_external",
        "pii_policy_version": PII_POLICY_VERSION,
        "injection_policy_version": INJECTION_POLICY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def rough_records(
    records: Iterable[dict[str, Any]], descriptor: dict[str, Any], source_uri: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach immutable lineage and split accepted/quarantine records."""
    accepted: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    source = descriptor["source"]
    for record in records:
        item = {
            "schema_version": 1,
            "record_id": digest(
                [
                    descriptor["input_id"],
                    source["version"],
                    record["page"],
                    record["paragraph"],
                    record["text"],
                ]
            ),
            "tenant_id": descriptor["tenant_id"],
            "input_id": descriptor["input_id"],
            "source_uri": source_uri,
            "source_version": source["version"],
            "content_hash": sha256_bytes(record["text"].encode("utf-8")),
            "locator": {"page": record["page"], "paragraph": record["paragraph"]},
            "acl": descriptor["acl"],
            "acl_digest": descriptor["acl_digest"],
            "trust_label": descriptor["trust_label"],
            "text": record["text"],
            "decision": "accepted" if not record["injection_codes"] else "quarantined",
            "reason_codes": record["injection_codes"],
        }
        (quarantined if item["decision"] == "quarantined" else accepted).append(item)
    return accepted, quarantined


def refine_records(
    records: list[dict[str, Any]], descriptor: dict[str, Any], source_uri: str
) -> dict[str, Any]:
    if not records:
        raise DocumentRejected("rough_corpus_empty")
    encoded_size = sum(len(json.dumps(record, ensure_ascii=False)) for record in records)
    if encoded_size > MAX_ROUGH_BYTES or len(records) > MAX_ROUGH_RECORDS:
        raise DocumentRejected("rough_corpus_limit")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if (
            record.get("decision") != "accepted"
            or record.get("tenant_id") != descriptor["tenant_id"]
        ):
            raise DocumentRejected("rough_record_not_accepted")
        if record.get("source_version") != descriptor["source"]["version"]:
            raise DocumentRejected("rough_source_version_mismatch")
        if record.get("acl_digest") != descriptor["acl_digest"]:
            raise DocumentRejected("rough_acl_mismatch")
        grouped.setdefault(record["source_version"], []).append(record)

    documents: list[dict[str, Any]] = []
    for source_version, source_records in grouped.items():
        spans = []
        for ordinal, record in enumerate(source_records):
            locator = record["locator"]
            spans.append(
                {
                    "schema_version": "canonical_span.v1",
                    "source_asset_id": descriptor["input_id"],
                    "source_uri": source_uri,
                    "source_version": source_version,
                    "span_id": digest(
                        [descriptor["input_id"], source_version, locator, record["content_hash"]]
                    ),
                    "parent_span_id": None,
                    "ordinal": ordinal,
                    "text": record["text"],
                    "locator": locator,
                    "structure": {
                        "title": None,
                        "section": None,
                        "page": locator.get("page"),
                        "paragraph": locator.get("paragraph"),
                        "content_type": "paragraph",
                    },
                    "tenant_id": descriptor["tenant_id"],
                    "acl_digest": descriptor["acl_digest"],
                    "trust_label": descriptor["trust_label"],
                    "content_sha256": record["content_hash"],
                    "pii_labels": [],
                    "parse_policy_version": PARSE_POLICY_VERSION,
                }
            )
        document = {
            "schema_version": "canonical_document.v1",
            "document_key": digest([descriptor["tenant_id"], source_uri, source_version]),
            "tenant_id": descriptor["tenant_id"],
            "source_uri": source_uri,
            "source_version": source_version,
            "content_hash": sha256_bytes("\n".join(span["text"] for span in spans).encode("utf-8")),
            "acl": descriptor["acl"],
            "acl_digest": descriptor["acl_digest"],
            "trust_label": descriptor["trust_label"],
            "spans": spans,
            "quality": {
                "pii_policy_version": PII_POLICY_VERSION,
                "injection_policy_version": INJECTION_POLICY_VERSION,
                "deduplicated": True,
            },
        }
        documents.append(document)
    result = {
        "schema_version": "canonical_content.v1",
        "input_id": descriptor["input_id"],
        "tenant_id": descriptor["tenant_id"],
        "source_uri": source_uri,
        "source_version": descriptor["source"]["version"],
        "documents": documents,
        "metrics": {
            "documents": len(documents),
            "spans": sum(len(item["spans"]) for item in documents),
        },
    }
    result["sha256"] = digest(result)
    return result


def build_rag_projection(canonical: dict[str, Any]) -> dict[str, Any]:
    """Build the only retrieval projection from versioned canonical spans."""
    chunker = RecursiveChunker()
    documents: list[dict[str, Any]] = []
    for document in canonical.get("documents", []):
        chunks: list[dict[str, Any]] = []
        for span in document.get("spans", []):
            for ordinal, chunk in enumerate(chunker.split(span["text"])):
                text = chunk["text"]
                chunks.append(
                    {
                        "rag_chunk_id": digest(
                            [span["span_id"], ordinal, sha256_bytes(text.encode("utf-8"))]
                        ),
                        "source_span_ids": [span["span_id"]],
                        "retrieval_text": text,
                        "parent_context": span["text"],
                        "locator": span["locator"],
                        "source_version": span["source_version"],
                        "source_content_sha256": span["content_sha256"],
                        "acl_digest": span["acl_digest"],
                        "chunk_policy_version": RAG_CHUNK_POLICY_VERSION,
                    }
                )
        documents.append(
            {
                "document_key": document["document_key"],
                "tenant_id": document["tenant_id"],
                "source_uri": document["source_uri"],
                "source_version": document["source_version"],
                "content_hash": document["content_hash"],
                "acl": document["acl"],
                "acl_digest": document["acl_digest"],
                "trust_label": document["trust_label"],
                "chunks": chunks,
            }
        )
    result = {
        "schema_version": "rag_projection.v1",
        "input_id": canonical["input_id"],
        "tenant_id": canonical["tenant_id"],
        "source_uri": canonical["source_uri"],
        "source_version": canonical["source_version"],
        "chunk_policy_version": RAG_CHUNK_POLICY_VERSION,
        "documents": documents,
        "metrics": {
            "documents": len(documents),
            "chunks": sum(len(item["chunks"]) for item in documents),
        },
    }
    result["sha256"] = digest(result)
    return result
