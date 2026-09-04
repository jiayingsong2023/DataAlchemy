"""PostgreSQL FTS + pgvector retrieval with CrossEncoder reranking."""

from __future__ import annotations

import os
import time
from typing import Any

from config import get_model_config
from rag.vector_store import VectorStore
from utils.logger import logger


def _load_cross_encoder(model_name: str, device: str) -> Any:
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name, device=device)


class Retriever:
    """Hybrid retrieval using PostgreSQL candidates and Reciprocal Rank Fusion."""

    def __init__(self, vector_store: VectorStore):
        self.vs = vector_store
        self.reranker: Any = None

    @staticmethod
    def _rrf(*rankings: list[dict[str, Any]], offset: int = 60) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for ranking in rankings:
            for rank, candidate in enumerate(ranking, start=1):
                item = merged.setdefault(candidate["chunk_id"], {**candidate, "rrf_score": 0.0})
                item["rrf_score"] += 1.0 / (offset + rank)
        return sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)

    def retrieve(
        self,
        query: str,
        identity: dict[str, str],
        top_k: int = 5,
        rerank: bool = True,
        source_version: str | None = None,
        document_ids: list[str] | None = None,
        timings: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        if timings is not None:
            timings.update(
                {
                    "embedding_ms": 0.0,
                    "vector_ms": 0.0,
                    "fts_ms": 0.0,
                    "fusion_ms": 0.0,
                    "reranker_ms": 0.0,
                }
            )
        # Reranking only helps when the relevant chunk survives first-stage recall.
        recall_k = max(top_k * 20, 20)
        scope = {"document_ids": document_ids} if document_ids is not None else {}
        vector = self.vs.search_vector(
            query,
            identity,
            top_k=recall_k,
            source_version=source_version,
            timings=timings,
            **scope,
        )
        text = self.vs.search_text(
            query,
            identity,
            top_k=recall_k,
            source_version=source_version,
            timings=timings,
            **scope,
        )
        started = time.perf_counter()
        candidates = self._rrf(vector, text)
        if timings is not None:
            timings["fusion_ms"] = (time.perf_counter() - started) * 1000
        if not candidates:
            return []
        if rerank and len(candidates) > 1:
            started = time.perf_counter()
            if self.reranker is None:
                model_b = get_model_config("model_b")
                reranker_path = model_b.get("reranker_path") or model_b.get(
                    "reranker_id", "BAAI/bge-reranker-base"
                )
                device = os.getenv("RERANKER_DEVICE", "cpu")
                if device not in {"cpu", "cuda"}:
                    raise ValueError("RERANKER_DEVICE must be 'cpu' or 'cuda'")
                logger.info("Loading BGE-Reranker model from: %s (%s)", reranker_path, device)
                self.reranker = _load_cross_encoder(reranker_path, device)
            for candidate, score in zip(
                candidates,
                self.reranker.predict([[query, item["text"]] for item in candidates]),
                strict=True,
            ):
                candidate["rerank_score"] = float(score)
            candidates.sort(key=lambda item: item["rerank_score"], reverse=True)
            if timings is not None:
                timings["reranker_ms"] = (time.perf_counter() - started) * 1000
        elif timings is not None:
            timings["reranker_ms"] = 0.0
        return candidates[:top_k]
