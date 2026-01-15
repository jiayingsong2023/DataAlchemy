from typing import List, Dict, Any
from rag.vector_store import VectorStore
from utils.logger import logger
from config import get_model_config
from rank_bm25 import BM25Okapi
import jieba
import torch
import pickle
import os
from sentence_transformers import CrossEncoder

class Retriever:
    """Agent C's core retriever: Hybrid (FAISS + BM25) + Cross-Encoder Rerank."""
    
    def __init__(self, vector_store: VectorStore):
        self.vs = vector_store
        self.bm25 = None
        self.doc_ids = [] # Store only IDs to save memory
        # Cross-Encoder for high-precision reranking
        self.reranker = None # Lazy load to save memory/显存

    def _init_bm25(self):
        """Initialize BM25 index from current vector store metadata or cache."""
        if self.bm25 is not None:
            return
            
        # 1. Try loading from local pickle cache
        if os.path.exists(self.vs.bm25_path):
            try:
                with open(self.vs.bm25_path, 'rb') as f:
                    data = pickle.load(f)
                    self.bm25 = data['bm25']
                    self.doc_ids = data['doc_ids']
                logger.info("Loaded lightweight BM25 index from local cache.")
                return
            except Exception as e:
                logger.warning(f"Failed to load BM25 cache: {e}")

        # 2. Fallback: Rebuild from SQLite
        if not self.vs.db_conn:
            self.vs._init_db()
            
        cursor = self.vs.db_conn.cursor()
        cursor.execute("SELECT id, text FROM metadata")
        rows = cursor.fetchall()
        
        if not rows:
            return
            
        doc_ids = []
        tokenized_corpus = []
        for r_id, text in rows:
            doc_ids.append(r_id)
            tokenized_corpus.append(list(jieba.cut(text)))
        
        self.bm25 = BM25Okapi(tokenized_corpus)
        self.doc_ids = doc_ids
        
        # 3. Save to cache for next time
        try:
            with open(self.vs.bm25_path, 'wb') as f:
                pickle.dump({'bm25': self.bm25, 'doc_ids': self.doc_ids}, f)
            logger.info("Saved lightweight BM25 index to local cache.")
        except Exception as e:
            logger.warning(f"Failed to save BM25 cache: {e}")

        logger.info(f"BM25 index initialized with {len(self.doc_ids)} document IDs.")

    def retrieve(self, query: str, top_k: int = 5, rerank: bool = True,
                 quant_enhancer=None) -> List[Dict[str, Any]]:
        """Hybrid retrieval entry point with optional Quant enhancement."""
        logger.info(f"Hybrid retrieving for: {query}")
        
        # 1. Vector Recall (FAISS)
        recall_k = 20 # Fetch more candidates for reranking
        vector_results = self.vs.search(query, top_k=recall_k)
        
        # 2. Keyword Recall (BM25)
        self._init_bm25()
        bm25_results = []
        if self.bm25:
            tokenized_query = list(jieba.cut(query))
            scores = self.bm25.get_scores(tokenized_query)
            top_n_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:recall_k]
            
            # Map indices to SQLite IDs
            target_ids = [self.doc_ids[i] for i in top_n_indices if scores[i] > 0]
            
            if target_ids:
                if not self.vs.db_conn:
                    self.vs._init_db()
                cursor = self.vs.db_conn.cursor()
                
                # Fetch actual content from SQLite
                placeholders = ', '.join(['?'] * len(target_ids))
                cursor.execute(f"SELECT id, text, source FROM metadata WHERE id IN ({placeholders})", target_ids)
                content_map = {r[0]: {"text": r[1], "source": r[2]} for r in cursor.fetchall()}
                
                # Re-assemble results in BM25 score order
                for idx, doc_id in enumerate(target_ids):
                    if doc_id in content_map:
                        doc = content_map[doc_id]
                        bm25_results.append({
                            "text": doc["text"],
                            "source": doc["source"],
                            "score": float(scores[top_n_indices[idx]]),
                            "method": "bm25"
                        })

        # 3. Merge Results (Deduplication)
        seen_texts = set()
        combined_candidates = []
        
        # Standardize vector results format for merging
        for res in vector_results:
            res["method"] = "vector"
            if res["text"] not in seen_texts:
                combined_candidates.append(res)
                seen_texts.add(res["text"])
        
        for res in bm25_results:
            if res["text"] not in seen_texts:
                combined_candidates.append(res)
                seen_texts.add(res["text"])

        if not combined_candidates:
            return []

        # 4. Deep Rerank (Cross-Encoder)
        if rerank and len(combined_candidates) > 1:
            # Lazy load reranker
            if self.reranker is None:
                model_b = get_model_config("model_b")
                reranker_path = model_b.get("reranker_path") or model_b.get("reranker_id", "BAAI/bge-reranker-base")
                logger.info(f"Loading BGE-Reranker model from: {reranker_path}")
                self.reranker = CrossEncoder(reranker_path, 
                                            device='cuda' if torch.cuda.is_available() else 'cpu')
            
            logger.info(f"Reranking {len(combined_candidates)} candidates...")
            pairs = [[query, cand["text"]] for cand in combined_candidates]
            rerank_scores = self.reranker.predict(pairs)
            
            for i, score in enumerate(rerank_scores):
                combined_candidates[i]["rerank_score"] = float(score)
            
            # Quant Enhancement: Boost scores with numerical insights
            if quant_enhancer:
                combined_candidates = quant_enhancer.boost_rerank_score(combined_candidates, query)
            else:
                # Sort by rerank score (descending)
                combined_candidates = sorted(combined_candidates, key=lambda x: x.get("rerank_score", -100), reverse=True)
        
        # Apply Quant filtering if available
        if quant_enhancer:
            combined_candidates = quant_enhancer.filter_by_quant_criteria(combined_candidates)
        
        return combined_candidates[:top_k]
