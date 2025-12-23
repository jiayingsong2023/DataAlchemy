import os
import faiss
import numpy as np
import pickle
import torch
import torch.distributed

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "is_initialized"):
    torch.distributed.is_initialized = lambda: False
if not hasattr(torch.distributed, "get_rank"):
    torch.distributed.get_rank = lambda: 0

from sentence_transformers import SentenceTransformer
from typing import List, Dict, Any

class VectorStore:
    """FAISS-based vector store manager for RAG."""
    
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5", 
                 index_path: str = "data/faiss_index.bin",
                 metadata_path: str = "data/metadata.pkl"):
        self.model_name = model_name
        self.index_path = index_path
        self.metadata_path = metadata_path
        self.model = None
        self.index = None
        self.metadata = [] # List of dicts: {"text": "...", "source": "..."}

    def _load_model(self):
        if self.model is None:
            print(f"Loading embedding model: {self.model_name}...")
            # In a real enterprise env, we might use a local path
            self.model = SentenceTransformer(self.model_name)
    
    def add_documents(self, documents: List[Dict[str, Any]]):
        """
        Add documents to the index.
        documents: List of {"text": "...", "metadata": {...}}
        """
        self._load_model()
        texts = [doc["text"] for doc in documents]
        embeddings = self.model.encode(texts, convert_to_numpy=True)
        
        dimension = embeddings.shape[1]
        if self.index is None:
            # Initialize FAISS Index (L2 distance or Inner Product)
            # bge models are usually normalized, so IndexFlatIP works well
            self.index = faiss.IndexFlatIP(dimension)
            
        # Add to index
        faiss.normalize_L2(embeddings)
        self.index.add(embeddings)
        
        # Store metadata
        self.metadata.extend(documents)
        print(f"Added {len(documents)} documents to index. Total: {self.index.ntotal}")

    def save(self):
        if self.index is not None:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            faiss.write_index(self.index, self.index_path)
            with open(self.metadata_path, "wb") as f:
                pickle.dump(self.metadata, f)
            print(f"Index saved to {self.index_path}")

    def load(self):
        if os.path.exists(self.index_path) and os.path.exists(self.metadata_path):
            self.index = faiss.read_index(self.index_path)
            with open(self.metadata_path, "rb") as f:
                self.metadata = pickle.load(f)
            print(f"Index loaded from {self.index_path}. Total: {self.index.ntotal}")
            return True
        return False

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        self._load_model()
        if self.index is None:
            if not self.load():
                return []
        
        query_embedding = self.model.encode([query], convert_to_numpy=True)
        faiss.normalize_L2(query_embedding)
        
        distances, indices = self.index.search(query_embedding, top_k)
        
        results = []
        for i, idx in enumerate(indices[0]):
            if idx != -1 and idx < len(self.metadata):
                res = self.metadata[idx].copy()
                res["score"] = float(distances[0][i])
                results.append(res)
        return results

