import os
import json
import asyncio
import threading
import time
from rag.vector_store import VectorStore
from rag.retriever import Retriever
from typing import List, Dict, Any
from utils.logger import logger
from openai import OpenAI
from config import LLM_CONFIG

from rag.quant_enhancer import QuantRAGEnhancer

class AgentC:
    """Agent C: The Knowledge Manager (RAG) with S3 Sync and SQLite."""
    
    def __init__(self, index_path="data/faiss_index.bin", sync_interval=300):
        self.vs = VectorStore(index_path=index_path)
        self.retriever = Retriever(self.vs)
        self.quant_enhancer = QuantRAGEnhancer()  # Initialize Quant enhancer
        self.sync_interval = sync_interval
        self._stop_sync = False
        self._sync_thread = None
        
        # LLM for Query Rewriting
        from utils.proxy import get_openai_client_kwargs
        client_kwargs = get_openai_client_kwargs()
        self.llm_client = OpenAI(
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
            **client_kwargs
        )
        
        # Initial load from S3
        logger.info("Initializing knowledge base...")
        if not self.vs.load(from_s3=True):
            # If S3 load fails (e.g. first run), clear any local stale files
            # this prevents "Total: 19" issues when S3 is supposed to be empty
            logger.info("Sync from S3 skipped or failed. Cleaning local stale cache if any.")
            self.vs.clear()

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
        Supports both local paths and S3 paths.
        """
        logger.info(f"Building index from {chunks_path}...")
        documents = []
        
        # 1. Handle S3 Path
        if chunks_path.startswith("s3a://") or chunks_path.startswith("s3://"):
            documents = self._read_from_s3(chunks_path)
        else:
            # 2. Handle Local Path
            if not os.path.exists(chunks_path):
                logger.warning(f"Local chunks path not found: {chunks_path}")
                return
                
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
            
            # Enrich documents with Quant metadata before indexing
            documents = self.quant_enhancer.enrich_metadata(documents)
            
            self.vs.add_documents(documents)
            self.vs.save(upload_to_s3=upload)
            
            # --- Backup raw chunks to S3 for visibility (if it was a local build) ---
            if upload and not (chunks_path.startswith("s3a://") or chunks_path.startswith("s3://")):
                try:
                    s3 = self.vs._get_s3_client()
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

    def _read_from_s3(self, s3_path: str) -> List[Dict[str, Any]]:
        """Download and parse JSONL files from S3/MinIO."""
        logger.info(f"[*] Reading RAG chunks from S3: {s3_path}")
        try:
            s3 = self.vs._get_s3_client()
            
            # Parse bucket and prefix
            path_parts = s3_path.replace("s3a://", "").replace("s3://", "").split("/")
            bucket = path_parts[0]
            prefix = "/".join(path_parts[1:])
            
            # Handle directory vs file (Spark often outputs a directory named with .jsonl)
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
            documents = []
            
            # If no direct match, try adding a slash (for directory)
            if 'Contents' not in response:
                response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
                
            for obj in response.get('Contents', []):
                if obj['Key'].endswith(".json") or obj['Key'].endswith(".jsonl"):
                    data = s3.get_object(Bucket=bucket, Key=obj['Key'])
                    for line in data['Body'].read().decode('utf-8').splitlines():
                        if line.strip():
                            documents.append(json.loads(line))
            return documents
        except Exception as e:
            logger.error(f"[!] S3 Read failed for RAG chunks: {e}")
            return []

    def query(self, text: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Retrieve relevant context for a query with optional LLM refinement."""
        # Ensure index is loaded
        if self.vs.index is None:
            self.vs.load()
            
        # Step 1: Query Rewriting/Expansion (Optional but recommended for complex queries)
        search_query = text
        try:
            logger.info(f"Refining query: {text}")
            response = self.llm_client.chat.completions.create(
                model=LLM_CONFIG["model"],
                messages=[
                    {"role": "system", "content": "你是一个检索优化专家。请将用户的提问改写为更适合在知识库中进行语义和关键词检索的短句或关键词列表。只输出改写后的结果，不要解释。"},
                    {"role": "user", "content": text}
                ],
                temperature=0.3,
                max_tokens=100
            )
            refined_query = response.choices[0].message.content.strip()
            if refined_query:
                logger.info(f"Refined search query: {refined_query}")
                search_query = refined_query
        except Exception as e:
            logger.warning(f"Query refinement failed: {e}. Using original text.")

        # Retrieve with Quant enhancement
        results = self.retriever.retrieve(search_query, top_k=top_k, rerank=True, 
                                         quant_enhancer=self.quant_enhancer)
        return results
