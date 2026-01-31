"""
Quant-RAG Integration: Enhance RAG retrieval with numerical insights.
"""
import os
from typing import Any, Dict, List, Optional

from config import PROCESSED_DATA_DIR
from utils.logger import logger


class QuantRAGEnhancer:
    """
    Enhances RAG retrieval with Quant numerical insights.
    Provides filtering, scoring, and metadata enrichment.
    """

    def __init__(self, quant_features_path: Optional[str] = None):
        """
        Initialize with path to Quant's final_features.parquet.
        """
        if quant_features_path is None:
            quant_features_path = os.path.join(PROCESSED_DATA_DIR, "quant", "final_features.parquet")
        self.quant_features_path = quant_features_path
        self.quant_df = None
        self._load_quant_features()

    def _load_quant_features(self):
        """Lazy load Quant features if available."""
        if os.path.exists(self.quant_features_path):
            try:
                from agents.quant.utils import scan_parquet_optimized
                self.quant_df = scan_parquet_optimized(self.quant_features_path).collect()
                logger.info(f"Loaded Quant features: {self.quant_df.shape}")
            except Exception as e:
                logger.warning(f"Failed to load Quant features: {e}")

    def enrich_metadata(self, documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Enrich document metadata with Quant feature tags.
        This is called during build_index to attach numerical insights.
        """
        if self.quant_df is None:
            return documents

        # Match documents with Quant features by source_name or other identifier
        # For now, we add a generic "quant_available" flag
        for doc in documents:
            metadata = doc.get("metadata", {})
            metadata["quant_enhanced"] = True
            metadata["quant_features_count"] = len(self.quant_df.columns) if self.quant_df is not None else 0
            doc["metadata"] = metadata

        return documents

    def filter_by_quant_criteria(self, candidates: List[Dict[str, Any]],
                                  min_quant_score: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Pre-filter RAG candidates based on Quant feature thresholds.
        Example: Only return documents from projects with risk_score < 0.8
        """
        if self.quant_df is None or not candidates:
            return candidates

        # This is a placeholder - in real implementation, you'd match documents
        # to Quant features by source/project ID and apply filters
        filtered = []
        for cand in candidates:
            # For now, we just pass through, but you could add logic like:
            # if cand.get("source") in high_risk_projects: continue
            filtered.append(cand)

        return filtered

    def boost_rerank_score(self, candidates: List[Dict[str, Any]],
                          query: str) -> List[Dict[str, Any]]:
        """
        Enhance Cross-Encoder rerank scores with Quant numerical insights.
        Formula: Final_Score = 0.7 * Rerank_Score + 0.3 * Quant_Insight_Score
        """
        if self.quant_df is None or not candidates:
            return candidates

        # Example: Boost documents that match Quant's high-priority features
        for cand in candidates:
            rerank_score = cand.get("rerank_score", 0.0)

            # Placeholder: In real implementation, you'd compute a Quant score
            # based on how well the document's source matches Quant's insights
            quant_boost = 0.0
            if cand.get("metadata", {}).get("quant_enhanced"):
                quant_boost = 0.1  # Small boost for Quant-enhanced docs

            # Weighted fusion
            final_score = 0.7 * rerank_score + 0.3 * quant_boost
            cand["final_score"] = final_score

        # Re-sort by final_score
        candidates.sort(key=lambda x: x.get("final_score", x.get("rerank_score", -100)), reverse=True)
        return candidates

    def get_quant_context_for_query(self, query: str) -> Optional[str]:
        """
        Extract relevant Quant insights as context for the query.
        Returns a summary string that can be appended to retrieved documents.
        """
        if self.quant_df is None:
            return None

        # Example: Return top 3 most relevant feature summaries
        # In real implementation, you'd use semantic matching or keyword extraction
        summary = f"[Quant Insights: {len(self.quant_df.columns)} features analyzed]"
        return summary
