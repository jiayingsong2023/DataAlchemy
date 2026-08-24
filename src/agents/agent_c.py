"""Agent C: tenant-scoped enterprise knowledge retrieval."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from openai import OpenAI

from config import DATABASE_URL, EXECUTION_MODE, LLM_CONFIG
from etl.sanitizers import sanitize_for_cloud
from memory.orchestrator import MemoryOrchestrator
from rag.quant_enhancer import QuantRAGEnhancer
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from utils.cloud_audit import observable_model_call, record_cloud_call
from utils.logger import logger
from utils.proxy import get_openai_client_kwargs
from utils.s3_utils import S3Utils


class AgentC:
    """Knowledge manager backed by PostgreSQL + pgvector, not local indexes."""

    def __init__(self):
        self.vs = VectorStore()
        self.retriever = Retriever(self.vs)
        self.memory = MemoryOrchestrator(DATABASE_URL, self.vs, self.retriever)
        self.quant_enhancer = QuantRAGEnhancer()
        self.s3 = S3Utils()
        client_kwargs = get_openai_client_kwargs()
        self.llm_client = (
            OpenAI(api_key=LLM_CONFIG["api_key"], base_url=LLM_CONFIG["base_url"], **client_kwargs)
            if EXECUTION_MODE == "cloud"
            else None
        )

    def start_background_sync(self) -> None:
        """Retained for Coordinator compatibility; PostgreSQL is immediately consistent."""
        logger.info("Knowledge retrieval uses PostgreSQL; no index sync thread is started")

    def stop_background_sync(self) -> None:
        return None

    def build_index(
        self, chunks_path: str, identity: dict[str, str] | None = None, upload: bool = True
    ) -> bool:
        """Ingest chunks into PostgreSQL and optionally preserve raw chunks in MinIO."""
        identity = identity or {"tenant_id": "default", "username": "system", "role": "admin"}
        documents = self._read_documents(chunks_path)
        if not documents:
            logger.error("No documents found to ingest")
            return False
        for document in documents:
            document.setdefault("source", chunks_path)
        documents = self.quant_enhancer.enrich_metadata(documents)
        self.vs.add_documents(documents, identity=identity)
        if upload and not chunks_path.startswith(("s3://", "s3a://")):
            self._backup_raw_chunks(chunks_path)
        logger.info("Ingested %s documents into PostgreSQL", len(documents))
        return True

    def _read_documents(self, chunks_path: str) -> list[dict[str, Any]]:
        if chunks_path.startswith(("s3://", "s3a://")):
            return self._read_from_s3(chunks_path)
        if not os.path.exists(chunks_path):
            return []
        paths = (
            [
                os.path.join(chunks_path, name)
                for name in os.listdir(chunks_path)
                if name.endswith(".json")
            ]
            if os.path.isdir(chunks_path)
            else [chunks_path]
        )
        documents: list[dict[str, Any]] = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                documents.extend(json.loads(line) for line in handle if line.strip())
        return documents

    def _read_from_s3(self, s3_path: str) -> list[dict[str, Any]]:
        bucket, *parts = s3_path.replace("s3a://", "").replace("s3://", "").split("/")
        source = self.s3 if bucket == self.s3.bucket else S3Utils(bucket=bucket)
        documents: list[dict[str, Any]] = []
        for obj in source.list_objects("/".join(parts)):
            if obj["Key"].endswith((".json", ".jsonl")):
                body = source.get_object_body(obj["Key"])
                if body:
                    documents.extend(
                        json.loads(line) for line in body.decode("utf-8").splitlines() if line
                    )
        return documents

    def _backup_raw_chunks(self, chunks_path: str) -> None:
        try:
            if os.path.isdir(chunks_path):
                for name in os.listdir(chunks_path):
                    if name.endswith(".json"):
                        self.s3.upload_file(
                            os.path.join(chunks_path, name), f"processed/chunks/{name}"
                        )
            else:
                self.s3.upload_file(chunks_path, "processed/rag_chunks.jsonl")
        except Exception as error:
            logger.warning("Could not back up raw chunks: %s", error)

    def query(
        self,
        text: str,
        identity: dict[str, str],
        top_k: int = 3,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve only content authorized for the caller's tenant and identity."""
        search_query = text
        if self.llm_client:
            messages = [
                {"role": "system", "content": "将问题改写为简短检索关键词。只输出结果。"},
                {"role": "user", "content": sanitize_for_cloud(text)},
            ]
            generation_config = {"temperature": 0.3, "max_tokens": 100}
            started = time.perf_counter()
            try:
                record_cloud_call("agent_c.query_rewrite", LLM_CONFIG["model"], ["query"])
                response = self.llm_client.chat.completions.create(
                    model=LLM_CONFIG["model"],
                    messages=messages,
                    **generation_config,
                )
                search_query = response.choices[0].message.content.strip() or text
                if trace_recorder:
                    trace_recorder(
                        observable_model_call(
                            component="agent_c.query_rewrite",
                            model=LLM_CONFIG["model"],
                            messages=messages,
                            response=search_query,
                            generation_config=generation_config,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            status="succeeded",
                            revision_or_digest=getattr(response, "model", None),
                            usage=response.usage.model_dump() if response.usage else None,
                            provider_request_id=getattr(response, "id", None),
                        )
                    )
            except Exception as error:
                if trace_recorder:
                    trace_recorder(
                        observable_model_call(
                            component="agent_c.query_rewrite",
                            model=LLM_CONFIG["model"],
                            messages=messages,
                            response=None,
                            generation_config=generation_config,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            status="failed",
                        )
                    )
                logger.warning("Query refinement failed: %s", error)
        return self.memory.context(search_query, identity)[:top_k]
