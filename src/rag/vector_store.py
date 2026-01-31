import json
import os
import sqlite3

import faiss
import torch
import torch.distributed

from utils.s3_utils import S3Utils

# Monkeypatch for ROCm Windows compatibility
# ... (same as before)
if not hasattr(torch.distributed, "is_initialized"):
    torch.distributed.is_initialized = lambda: False
if not hasattr(torch.distributed, "get_rank"):
    torch.distributed.get_rank = lambda: 0

from typing import Any, Dict, List

from sentence_transformers import SentenceTransformer

from config import S3_BUCKET, get_model_config
from utils.logger import logger


class VectorStore:
    """FAISS-based vector store manager with SQLite metadata and S3 persistence."""

    def __init__(self, model_name: str = None,
                 index_path: str = "data/faiss_index.bin",
                 metadata_path: str = "data/metadata.db",
                 bm25_path: str = "data/bm25_index.pkl",
                 s3_bucket: str = None,
                 s3_prefix: str = "knowledge"):
        model_b = get_model_config("model_b")
        self.model_name = model_name or model_b.get("model_id", "BAAI/bge-small-zh-v1.5")
        self.device = model_b.get("device", "auto")
        self.index_path = index_path
        self.metadata_path = metadata_path
        self.bm25_path = bm25_path
        self.s3_bucket = s3_bucket or S3_BUCKET
        self.s3_prefix = s3_prefix

        self.model = None
        self.index = None
        self.db_conn = None

        # Unified S3 Utility
        self.s3 = S3Utils(bucket=self.s3_bucket)

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
            # Check if local model path exists in config
            model_b_config = get_model_config("model_b")
            local_path = model_b_config.get("model_path")

            # Use environment variable as override if set
            hf_offline = os.getenv("TRANSFORMERS_OFFLINE") == "1"

            if (local_path and os.path.exists(local_path)) or hf_offline:
                load_path = local_path if local_path and os.path.exists(local_path) else self.model_name
                logger.info(f"Loading embedding model from LOCAL (Offline Mode): {load_path}...")
                self.model = SentenceTransformer(load_path, local_files_only=True)
            else:
                logger.info(f"Loading embedding model from HF: {self.model_name}...")
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
        """Save index and metadata locally, optionally upload to S3."""
        if self.index is not None:
            os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
            faiss.write_index(self.index, self.index_path)
            logger.info(f"Index saved locally to {self.index_path}")

            # Close connection to ensure DB is flushed to disk before upload
            if self.db_conn:
                self.db_conn.commit()
                self.db_conn.close()
                self.db_conn = None

            if upload_to_s3:
                logger.info("Starting S3 sync...")
                if self.upload_to_s3():
                    logger.info("S3 sync completed successfully.")
                else:
                    logger.warning("S3 sync failed.")

    def upload_to_s3(self):
        """Upload local index and DB to S3, creating bucket if needed."""
        try:
            # Ensure bucket exists
            self.s3.ensure_bucket()

            self.s3.upload_file(self.index_path, f"{self.s3_prefix}/faiss_index.bin")

            # Upload BM25 if exists
            if os.path.exists(self.bm25_path):
                self.s3.upload_file(self.bm25_path, f"{self.s3_prefix}/bm25_index.pkl")

            # Close connection before uploading DB to ensure consistency
            if self.db_conn:
                self.db_conn.close()
                self.db_conn = None
            self.s3.upload_file(self.metadata_path, f"{self.s3_prefix}/metadata.db")
            logger.info(f"Successfully uploaded index and metadata to s3://{self.s3_bucket}/{self.s3_prefix}/")
            return True
        except Exception as e:
            logger.error(f"S3 upload failed: {e}", exc_info=True)
            return False

    def download_from_s3(self) -> bool:
        """Download index and DB from S3, only if they exist."""
        try:
            # Check if index exists first to avoid 404 error logs
            if not self.s3.exists(f"{self.s3_prefix}/faiss_index.bin"):
                logger.info(f"No existing knowledge index found in S3 bucket '{self.s3_bucket}'. This may be the first run.")
                return False

            logger.info(f"Downloading knowledge from S3 (Bucket: {self.s3_bucket})...")
            self.s3.download_file(f"{self.s3_prefix}/faiss_index.bin", self.index_path)

            # Metadata and BM25 are checked for existence to avoid noisy error logs
            for s3_key, local_path in [
                (f"{self.s3_prefix}/metadata.db", self.metadata_path),
                (f"{self.s3_prefix}/bm25_index.pkl", self.bm25_path)
            ]:
                if self.s3.exists(s3_key):
                    self.s3.download_file(s3_key, local_path)
                else:
                    logger.warning(f"Optional knowledge file {s3_key} not found on S3. Skipping.")

            return True
        except Exception as e:
            logger.error(f"S3 download failed: {e}")
            return False

    def clear(self):
        """Clear local index and metadata."""
        logger.info("Clearing local index and metadata...")
        if self.db_conn:
            self.db_conn.close()
            self.db_conn = None

        for p in [self.index_path, self.metadata_path, self.bm25_path]:
            if os.path.exists(p):
                os.remove(p)

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
