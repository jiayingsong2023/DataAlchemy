"""Shared admission and cleanup rules for Git content before it becomes retrievable."""

from __future__ import annotations

import os
import re
from typing import Any

from rag.chunkers.markdown import MarkdownChunker
from rag.chunkers.recursive import RecursiveChunker

_SKIPPED_PARTS = {".git", "node_modules", "vendor", "dist", "build", "target", "coverage"}
_SKIPPED_SUFFIXES = {".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip"}
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|"
    r"(?i:(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,})"
)


def prepare_git_document(
    filename: str,
    raw: bytes,
    source: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any] | None, object | None, str | None]:
    """Return a safe, normalized document or a stable rejection reason."""
    suffix = os.path.splitext(filename.lower())[1]
    parts = set(filename.replace("\\", "/").split("/"))
    if parts & _SKIPPED_PARTS or suffix in _SKIPPED_SUFFIXES:
        return None, None, "excluded_path_or_type"
    if len(raw) > int(os.getenv("GIT_MAX_INDEX_BYTES", "1048576")):
        return None, None, "index_size_limit"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "non_utf8"
    if "\x00" in text:
        return None, None, "binary_content"
    if _SECRET.search(text):
        return None, None, "secret_detected"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if not text:
        return None, None, "empty_content"
    chunker = MarkdownChunker() if suffix in {".md", ".markdown"} else RecursiveChunker()
    return {"text": text, "source": source, "metadata": metadata}, chunker, None
