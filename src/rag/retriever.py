from typing import List, Dict, Any
from rag.vector_store import VectorStore

class Retriever:
    """Agent C's core retriever: Recall + Rerank."""
    
    def __init__(self, vector_store: VectorStore):
        self.vs = vector_store
        # In a production env, we would use a CrossEncoder for reranking
        # self.reranker = CrossEncoder('BAAI/bge-reranker-base')
        self.reranker = None 

    def retrieve(self, query: str, top_k: int = 5, rerank: bool = False) -> List[Dict[str, Any]]:
        """Main retrieval entry point."""
        print(f"[Agent C] Retrieving knowledge for: {query}")
        
        # 1. Recall from FAISS
        # We fetch more than top_k if we want to rerank
        recall_k = top_k * 3 if rerank else top_k
        docs = self.vs.search(query, top_k=recall_k)
        
        if not docs:
            return []

        # 2. Rerank (Optional)
        if rerank and len(docs) > 1:
            print(f"[Agent C] Reranking {len(docs)} documents...")
            # For this demo, we use the vector similarity scores as is
            # In a full implementation, you'd use a Cross-Encoder here
            docs = sorted(docs, key=lambda x: x["score"], reverse=True)[:top_k]
        
        return docs[:top_k]

