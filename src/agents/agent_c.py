import os
import json
import asyncio
import threading
import time
from rag.vector_store import VectorStore
from rag.retriever import Retriever
from typing import List, Dict, Any
from utils.logger import logger

class AgentC:
    """Agent C: The Knowledge Manager (RAG) with S3 Sync and SQLite."""
    
    def __init__(self, index_path="data/faiss_index.bin", sync_interval=300):
        self.vs = VectorStore(index_path=index_path)
        self.retriever = Retriever(self.vs)
        self.sync_interval = sync_interval
        self._stop_sync = False
        self._sync_thread = None
        
        # Initial load from S3
        logger.info("Initializing knowledge base...")
        self.vs.load(from_s3=True)

    def start_background_sync(self):
        """Start a background thread to periodically sync with S3."""
        if self._sync_thread is None:
            self._stop_sync = False
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self._sync_thread.start()
            logger.info(f"Background sync started (interval: {self.sync_interval}s)")

    def stop_background_sync(self):
        """Stop the background sync thread."""
        self._stop_sync = True
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
            self._sync_thread = None

    def _sync_loop(self):
        """Periodic sync loop."""
        while not self._stop_sync:
            try:
                time.sleep(self.sync_interval)
                if self._stop_sync:
                    break
                logger.info("Periodic sync check...")
                # In a real scenario, we might check S3 ETag/LastModified first
                # For now, we just reload
                self.vs.load(from_s3=True)
            except Exception as e:
                logger.error(f"Sync error: {e}", exc_info=True)

    def build_index(self, chunks_path: str, upload: bool = True):
        """Build FAISS index from cleaned chunks and upload to S3.
        Supports both single file and directory (Spark output).
        """
        if not os.path.exists(chunks_path):
            logger.warning(f"Chunks file not found: {chunks_path}")
            return
            
        logger.info(f"Building index from {chunks_path}...")
        documents = []
        
        # Helper to read from a single file
        def read_file(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        documents.append(json.loads(line))

        if os.path.isdir(chunks_path):
            for filename in os.listdir(chunks_path):
                if filename.startswith("part-") and filename.endswith(".json"):
                    read_file(os.path.join(chunks_path, filename))
        else:
            read_file(chunks_path)
        
        if documents:
            # Clear existing local data for a fresh build
            self.vs.clear()
            self.vs.add_documents(documents)
            self.vs.save(upload_to_s3=upload)
            
            # --- NEW: Backup raw chunks to S3 for visibility ---
            if upload:
                try:
                    s3 = self.vs._get_s3_client()
                    # Determine if it's a directory (Spark) or file
                    if os.path.isdir(chunks_path):
                        for f in os.listdir(chunks_path):
                            if f.startswith("part-") and f.endswith(".json"):
                                s3.upload_file(os.path.join(chunks_path, f), 
                                               self.vs.s3_bucket, 
                                               f"processed/chunks/{f}")
                    else:
                        s3.upload_file(chunks_path, self.vs.s3_bucket, "processed/rag_chunks.jsonl")
                    logger.info("Raw chunks backed up to S3: processed/chunks/")
                except Exception as e:
                    logger.warning(f"Failed to backup chunks to S3: {e}")
            
            logger.info("Index built and synced to S3 successfully.")
        else:
            logger.warning("No documents found to index.")

    def query(self, text: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Retrieve relevant context for a query."""
        # Ensure index is loaded
        if self.vs.index is None:
            self.vs.load()
            
        return self.retriever.retrieve(text, top_k=top_k, rerank=True)
