"""PostgreSQL FTS + pgvector retrieval with CrossEncoder reranking."""

from __future__ import annotations

import os
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
        quant_enhancer: Any = None,
        source_version: str | None = None,
    ) -> list[dict[str, Any]]:
        # Reranking only helps when the relevant chunk survives first-stage recall.
        recall_k = max(top_k * 20, 20)
        candidates = self._rrf(
            self.vs.search_vector(
                query, identity, top_k=recall_k, source_version=source_version
            ),
            self.vs.search_text(query, identity, top_k=recall_k, source_version=source_version),
        )
        if not candidates:
            return []
        if rerank and len(candidates) > 1:
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
        if quant_enhancer:
            candidates = quant_enhancer.filter_by_quant_criteria(candidates)
        return candidates[:top_k]
