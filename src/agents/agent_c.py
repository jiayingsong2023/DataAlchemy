import os
import json
from rag.vector_store import VectorStore
from rag.retriever import Retriever
from typing import List, Dict, Any

class AgentC:
    """Agent C: The Knowledge Manager (RAG)."""
    
    def __init__(self, index_path="data/faiss_index.bin"):
        self.vs = VectorStore(index_path=index_path)
        self.retriever = Retriever(self.vs)

    def build_index(self, chunks_path: str):
        """Build FAISS index from cleaned chunks."""
        if not os.path.exists(chunks_path):
            print(f"[Agent C] Chunks file not found: {chunks_path}")
            return
            
        print(f"[Agent C] Building index from {chunks_path}...")
        documents = []
        with open(chunks_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    documents.append(json.loads(line))
        
        if documents:
            self.vs.add_documents(documents)
            self.vs.save()
            print("[Agent C] Index built and saved successfully.")
        else:
            print("[Agent C] No documents found to index.")

    def query(self, text: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Retrieve relevant context for a query."""
        if self.vs.index is None:
            self.vs.load()
        return self.retriever.retrieve(text, top_k=top_k, rerank=True)

