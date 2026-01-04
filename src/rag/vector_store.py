import os
import faiss
import numpy as np
import sqlite3
import json
import torch
import torch.distributed
import boto3
from botocore.client import Config

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "is_initialized"):
    torch.distributed.is_initialized = lambda: False
if not hasattr(torch.distributed, "get_rank"):
    torch.distributed.get_rank = lambda: 0

from sentence_transformers import SentenceTransformer
from typing import List, Dict, Any, Optional
from config import get_model_config

class VectorStore:
    """FAISS-based vector store manager with SQLite metadata and S3 persistence."""
    
    def __init__(self, model_name: str = None, 
                 index_path: str = "data/faiss_index.bin",
                 metadata_path: str = "data/metadata.db",
                 s3_bucket: str = "lora-data",
                 s3_prefix: str = "knowledge"):
        model_b = get_model_config("model_b")
        self.model_name = model_name or model_b.get("model_id", "BAAI/bge-small-zh-v1.5")
        self.device = model_b.get("device", "auto")
        self.index_path = index_path
        self.metadata_path = metadata_path
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        
        self.model = None
        self.index = None
        self.db_conn = None
        
        # MinIO/S3 Config
        self.s3_endpoint = "http://localhost:9000"
        self.s3_access_key = "minioadmin"
        self.s3_secret_key = "minioadmin"

    def _get_s3_client(self):
        return boto3.client('s3',
                            endpoint_url=self.s3_endpoint,
                            aws_access_key_id=self.s3_access_key,
                            aws_secret_access_key=self.s3_secret_key,
                            config=Config(signature_version='s3v4'),
                            region_name='us-east-1')

    def _init_db(self):
        """Initialize SQLite database for metadata."""
        if self.db_conn is None:
            os.makedirs(os.path.dirname(self.metadata_path), exist_ok=True)
            self.db_conn = sqlite3.connect(self.metadata_path, check_same_thread=False)
            # Enable WAL mode for better concurrency
            self.db_conn.execute("PRAGMA journal_mode=WAL;")
            self.db_conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    id INTEGER PRIMARY KEY,
                    text TEXT,
                    source TEXT,
                    extra TEXT
                )
            """)
            self.db_conn.commit()

    def _load_model(self):
        if self.model is None:
            print(f"Loading embedding model: {self.model_name}...")
            self.model = SentenceTransformer(self.model_name)
    
    def add_documents(self, documents: List[Dict[str, Any]]):
        """Add documents to FAISS and SQLite."""
        self._load_model()
        self._init_db()
        
        texts = [doc["text"] for doc in documents]
        embeddings = self.model.encode(texts, convert_to_numpy=True)
        faiss.normalize_L2(embeddings)
        
        dimension = embeddings.shape[1]
        if self.index is None:
            self.index = faiss.IndexFlatIP(dimension)
            
        # Add to FAISS
        start_idx = self.index.ntotal
        self.index.add(embeddings)
        
        # Add to SQLite
        cursor = self.db_conn.cursor()
        for i, doc in enumerate(documents):
            cursor.execute(
                "INSERT INTO metadata (id, text, source, extra) VALUES (?, ?, ?, ?)",
                (start_idx + i, doc["text"], doc.get("source", ""), json.dumps(doc.get("metadata", {})))
            )
        self.db_conn.commit()
        print(f"Added {len(documents)} documents. Total: {self.index.ntotal}")

    def save(self, upload_to_s3: bool = False):
        """Save index and metadata locally, optionally upload to S3."""
        if self.index is not None:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            faiss.write_index(self.index, self.index_path)
            print(f"Index saved locally to {self.index_path}")
            
            if upload_to_s3:
                self.upload_to_s3()

    def upload_to_s3(self):
        """Upload local index and DB to S3."""
        try:
            s3 = self._get_s3_client()
            s3.upload_file(self.index_path, self.s3_bucket, f"{self.s3_prefix}/faiss_index.bin")
            # Close connection before uploading DB to ensure consistency
            if self.db_conn:
                self.db_conn.close()
                self.db_conn = None
            s3.upload_file(self.metadata_path, self.s3_bucket, f"{self.s3_prefix}/metadata.db")
            print(f"[VectorStore] Uploaded index and metadata to s3://{self.s3_bucket}/{self.s3_prefix}/")
        except Exception as e:
            print(f"[VectorStore] S3 upload failed: {e}")

    def download_from_s3(self) -> bool:
        """Download index and DB from S3."""
        try:
            s3 = self._get_s3_client()
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            
            print(f"[VectorStore] Downloading knowledge from S3...")
            s3.download_file(self.s3_bucket, f"{self.s3_prefix}/faiss_index.bin", self.index_path)
            s3.download_file(self.s3_bucket, f"{self.s3_prefix}/metadata.db", self.metadata_path)
            return True
        except Exception as e:
            print(f"[VectorStore] S3 download failed: {e}")
            return False

    def load(self, from_s3: bool = False):
        """Load index and open DB connection."""
        if from_s3:
            if not self.download_from_s3():
                print("[VectorStore] Failed to download from S3, trying local...")
            
        if os.path.exists(self.index_path) and os.path.exists(self.metadata_path):
            self.index = faiss.read_index(self.index_path)
            self._init_db()
            print(f"Index loaded. Total: {self.index.ntotal}")
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
        
        self._init_db()
        cursor = self.db_conn.cursor()
        results = []
        for i, idx in enumerate(indices[0]):
            if idx != -1:
                cursor.execute("SELECT text, source, extra FROM metadata WHERE id = ?", (int(idx),))
                row = cursor.fetchone()
                if row:
                    results.append({
                        "text": row[0],
                        "source": row[1],
                        "metadata": json.loads(row[2]),
                        "score": float(distances[0][i])
                    })
        return results
