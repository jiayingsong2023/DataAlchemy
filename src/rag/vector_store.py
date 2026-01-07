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
from config import get_model_config, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET
from utils.logger import logger

class VectorStore:
    """FAISS-based vector store manager with SQLite metadata and S3 persistence."""
    
    def __init__(self, model_name: str = None, 
                 index_path: str = "data/faiss_index.bin",
                 metadata_path: str = "data/metadata.db",
                 s3_bucket: str = None,
                 s3_prefix: str = "knowledge"):
        model_b = get_model_config("model_b")
        self.model_name = model_name or model_b.get("model_id", "BAAI/bge-small-zh-v1.5")
        self.device = model_b.get("device", "auto")
        self.index_path = index_path
        self.metadata_path = metadata_path
        self.s3_bucket = s3_bucket or S3_BUCKET
        self.s3_prefix = s3_prefix
        
        self.model = None
        self.index = None
        self.db_conn = None
        
        # MinIO/S3 Config
        self.s3_endpoint = S3_ENDPOINT
        self.s3_access_key = S3_ACCESS_KEY
        self.s3_secret_key = S3_SECRET_KEY

    def _get_s3_client(self):
        return boto3.client('s3',
                            endpoint_url=self.s3_endpoint,
                            aws_access_key_id=self.s3_access_key,
                            aws_secret_access_key=self.s3_secret_key,
                            config=Config(
                                signature_version='s3v4',
                                s3={'addressing_style': 'path'},
                                retries={'max_attempts': 10, 'mode': 'standard'} # 增强重试
                            ),
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
            logger.info(f"Loading embedding model: {self.model_name}...")
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
        logger.info(f"Added {len(documents)} documents. Total: {self.index.ntotal}")

    def save(self, upload_to_s3: bool = False):
        """Save index and metadata locally, optionally upload to S3 in background."""
        if self.index is not None:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            faiss.write_index(self.index, self.index_path)
            logger.info(f"Index saved locally to {self.index_path}")
            
            if upload_to_s3:
                import threading
                # 使用线程异步上传，不阻塞主流程
                def _async_upload():
                    if self.upload_to_s3():
                        logger.info("Background S3 sync completed successfully.")
                    else:
                        logger.warning("Background S3 sync failed.")
                
                upload_thread = threading.Thread(target=_async_upload)
                upload_thread.start()
                logger.info("S3 sync started in background...")

    def upload_to_s3(self):
        """Upload local index and DB to S3, creating bucket if needed."""
        try:
            s3 = self._get_s3_client()
            
            # Ensure bucket exists
            try:
                s3.head_bucket(Bucket=self.s3_bucket)
            except:
                logger.info(f"Creating missing bucket: {self.s3_bucket}")
                s3.create_bucket(Bucket=self.s3_bucket)

            s3.upload_file(self.index_path, self.s3_bucket, f"{self.s3_prefix}/faiss_index.bin")
            # Close connection before uploading DB to ensure consistency
            if self.db_conn:
                self.db_conn.close()
                self.db_conn = None
            s3.upload_file(self.metadata_path, self.s3_bucket, f"{self.s3_prefix}/metadata.db")
            logger.info(f"Successfully uploaded index and metadata to s3://{self.s3_bucket}/{self.s3_prefix}/")
            return True
        except Exception as e:
            logger.error(f"S3 upload failed: {e}", exc_info=True)
            return False

    def download_from_s3(self) -> bool:
        """Download index and DB from S3."""
        try:
            s3 = self._get_s3_client()
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            
            logger.info(f"Downloading knowledge from S3 (Bucket: {self.s3_bucket})...")
            s3.download_file(self.s3_bucket, f"{self.s3_prefix}/faiss_index.bin", self.index_path)
            s3.download_file(self.s3_bucket, f"{self.s3_prefix}/metadata.db", self.metadata_path)
            return True
        except Exception as e:
            logger.error(f"S3 download failed: {e}")
            logger.info(f"  Hint: Ensure MinIO is running at {self.s3_endpoint} and bucket '{self.s3_bucket}' exists.")
            return False

    def clear(self):
        """Clear local index and metadata."""
        logger.info("Clearing local index and metadata...")
        if self.db_conn:
            self.db_conn.close()
            self.db_conn = None
        
        if os.path.exists(self.index_path):
            os.remove(self.index_path)
        if os.path.exists(self.metadata_path):
            os.remove(self.metadata_path)
        
        # Also remove WAL files if they exist
        for suffix in ["-shm", "-wal"]:
            if os.path.exists(self.metadata_path + suffix):
                os.remove(self.metadata_path + suffix)
                
        self.index = None

    def load(self, from_s3: bool = False):
        """Load index and open DB connection."""
        if from_s3:
            if not self.download_from_s3():
                logger.warning("Failed to download from S3, trying local...")
            
        if os.path.exists(self.index_path) and os.path.exists(self.metadata_path):
            self.index = faiss.read_index(self.index_path)
            self._init_db()
            logger.info(f"Index loaded. Total: {self.index.ntotal}")
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
