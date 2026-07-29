import hashlib
import json
import os
from typing import Any, Dict, List, Optional

import numpy as np
import redis.asyncio as redis

# Walkaround for torch.distributed compatibility issues (e.g. on Windows or specific ROCm builds)
# sentence-transformers checks for torch.distributed.is_initialized()
import torch

if not hasattr(torch, 'distributed'):
    import types
    torch.distributed = types.ModuleType("torch.distributed")
if not hasattr(torch.distributed, 'is_initialized'):
    torch.distributed.is_initialized = lambda: False

import time

from sentence_transformers import SentenceTransformer

from config import REDIS_URL, get_model_config
from utils.logger import logger

from .metrics import MetricsManager


class CacheManager:
    """
    Advanced Cache Manager supporting Redis persistence and Semantic Search.
    """

    def __init__(
        self,
        redis_url: str = None,
        enable_semantic: bool = True,
        semantic_threshold: float = 0.92,
        embedding_model: str = None,
        prefix: str = "dataalchemy",
    ):
        self.redis_url = redis_url or REDIS_URL
        self.redis: Optional[redis.Redis] = None
        self.enable_semantic = enable_semantic
        self.semantic_threshold = semantic_threshold
        self.prefix = prefix.rstrip(":")

        # Lazy load embedding model
        model_b = get_model_config("model_b")
        # Priority: explicit arg > model_path > model_id
        self.embedding_model_name = embedding_model or model_b.get("model_path") or model_b.get("model_id", "BAAI/bge-small-zh-v1.5")
        self.model = None

        # Local semantic index (for simple vector search)
        self.semantic_index: List[Dict[str, Any]] = []
        self.semantic_redis_key = f"{self.prefix}:cache:semantic:index"

        logger.info(f"CacheManager initialized (Redis: {redis_url}, Semantic: {enable_semantic})")

    async def connect(self):
        """Connect to Redis and load semantic index"""
        if self.redis is None:
            try:
                self.redis = redis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                logger.info("Connected to Redis")

                # Load semantic index from Redis
                if self.enable_semantic:
                    await self._load_semantic_index()
            except Exception as e:
                logger.error(f"Redis connection failed: {e}")
                self.redis = None

    def _get_exact_key(self, prompt: str, kwargs: Dict, scope: str) -> str:
        """Create a unique key for exact match"""
        # Sort kwargs to ensure consistent hashing
        kwargs_str = json.dumps(kwargs, sort_keys=True)
        combined = f"{scope}||{prompt}||{kwargs_str}"
        return f"{self.prefix}:cache:exact:{hashlib.md5(combined.encode()).hexdigest()}"

    async def get(self, prompt: str, generation_kwargs: Dict, scope: Optional[str] = None) -> Optional[str]:
        """Get result from cache (Exact -> Semantic)"""
        if not scope:
            return None
        if self.redis is None:
            await self.connect()

        if self.redis is None:
            return None

        # 1. Try Exact Match
        exact_key = self._get_exact_key(prompt, generation_kwargs, scope)
        cached = await self.redis.get(exact_key)
        if cached:
            logger.info("Exact match hit!")
            MetricsManager.record_cache_hit("exact")
            return cached

        # 2. Try Semantic Match (if enabled)
        if self.enable_semantic:
            res = await self._get_semantic(prompt, scope)
            if res:
                MetricsManager.record_cache_hit("semantic")
            return res

        return None

    async def set(
        self, prompt: str, generation_kwargs: Dict, result: str, scope: Optional[str] = None
    ):
        """Store result in cache"""
        if not scope:
            return
        if self.redis is None:
            await self.connect()

        if self.redis is None:
            return

        # 1. Store Exact Match (TTL: 24 hours)
        exact_key = self._get_exact_key(prompt, generation_kwargs, scope)
        await self.redis.setex(exact_key, 86400, result)

        # 2. Update Semantic Index
        if self.enable_semantic:
            await self._add_semantic(prompt, result, scope)

    async def _get_semantic(self, prompt: str, scope: str) -> Optional[str]:
        """Simple semantic search implementation with dimension robustness"""
        if not self.semantic_index:
            return None

        if self.model is None:
            logger.info(f"Loading embedding model for cache: {self.embedding_model_name}")
            # Ensure local_files_only if it's a path or in offline mode
            is_path = os.path.isdir(self.embedding_model_name)
            hf_offline = os.getenv("TRANSFORMERS_OFFLINE") == "1"

            try:
                self.model = SentenceTransformer(
                    self.embedding_model_name,
                    local_files_only=is_path or hf_offline
                )
            except Exception as e:
                logger.error(f"Failed to load embedding model {self.embedding_model_name}: {e}")
                # Fallback to online if not in strict offline mode
                if not hf_offline:
                    self.model = SentenceTransformer(self.embedding_model_name)
                else:
                    raise e

        # Generate embedding for query
        query_vec = self.model.encode([prompt])[0]

        best_score = -1
        best_result = None

        # Simple cosine similarity search
        for item in self.semantic_index:
            if item["scope"] != scope:
                continue
            # Robustness check: Ensure dimensions match (e.g. 512 vs 384 after model upgrade)
            if query_vec.shape != item["vector"].shape:
                continue

            score = np.dot(query_vec, item["vector"]) / (
                np.linalg.norm(query_vec) * np.linalg.norm(item["vector"])
            )
            if score > best_score:
                best_score = score
                best_result = item["result"]

        if best_score >= self.semantic_threshold:
            logger.info(f"Semantic hit! (Score: {best_score:.4f})")
            return best_result

        return None

    async def _add_semantic(self, prompt: str, result: str, scope: str):
        """Add entry to semantic index and persist to Redis"""
        if self.model is None:
            # We use the same loading logic as _get_semantic
            # Initialize by calling _get_semantic (it will load the model)
            # or just repeat the logic here for clarity if needed.
            # But here we'll just use a small helper or repeat.
            is_path = os.path.isdir(self.embedding_model_name)
            hf_offline = os.getenv("TRANSFORMERS_OFFLINE") == "1"
            self.model = SentenceTransformer(
                self.embedding_model_name,
                local_files_only=is_path or hf_offline
            )

        vector = self.model.encode([prompt])[0]
        entry = {
            "vector": vector.tolist(), # Convert to list for JSON serialization
            "result": result,
            "prompt": prompt,
            "scope": scope,
        }
        self.semantic_index.append({
            "vector": vector, # Keep as numpy for local search
            "result": result,
            "prompt": prompt,
            "scope": scope,
        })

        # Keep index size manageable
        if len(self.semantic_index) > 1000:
            self.semantic_index.pop(0)

        # Persist to Redis (simple list push)
        if self.redis:
            await self.redis.rpush(self.semantic_redis_key, json.dumps(entry))
            # Trim Redis list too
            await self.redis.ltrim(self.semantic_redis_key, -1000, -1)

    async def _load_semantic_index(self):
        """Load semantic index from Redis"""
        if not self.redis:
            return

        logger.info("Loading semantic index from Redis...")
        data = await self.redis.lrange(self.semantic_redis_key, 0, -1)
        self.semantic_index = []
        for item_str in data:
            item = json.loads(item_str)
            self.semantic_index.append({
                "vector": np.array(item["vector"]),
                "result": item["result"],
                "prompt": item["prompt"],
                "scope": item.get("scope"),
            })
        logger.info(f"Loaded {len(self.semantic_index)} semantic entries")

    async def clear(self):
        """Clear only DataAlchemy-owned cache and session keys."""
        if self.redis:
            keys = [key async for key in self.redis.scan_iter(f"{self.prefix}:*")]
            if keys:
                await self.redis.delete(*keys)
        self.semantic_index = []
        logger.info("Cache cleared")

    # --- Session & History Management (Refactored for Phase 8) ---

    def _get_user_sessions_key(self, tenant_id: str, username: str) -> str:
        """Key for the list of session IDs belonging to a user"""
        return f"{self.prefix}:tenant:{tenant_id}:user:{username}:sessions"

    def _get_session_meta_key(self, session_id: str) -> str:
        """Key for session metadata (title, created_at, etc.)"""
        return f"{self.prefix}:session:{session_id}:meta"

    def _get_session_messages_key(self, session_id: str) -> str:
        """Key for the list of messages in a session"""
        return f"{self.prefix}:session:{session_id}:messages"

    async def create_session(
        self, username: str, title: str = "New Chat", tenant_id: str = "default"
    ) -> str:
        """Create a new session and return its ID"""
        if not self.redis: await self.connect()

        session_id = hashlib.md5(f"{tenant_id}:{username}:{time.time()}".encode()).hexdigest()[:12]

        # 1. Add to user's session list
        await self.redis.rpush(self._get_user_sessions_key(tenant_id, username), session_id)

        # 2. Store metadata
        meta = {
            "id": session_id,
            "owner": username,
            "tenant_id": tenant_id,
            "title": title,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        await self.redis.set(self._get_session_meta_key(session_id), json.dumps(meta))

        return session_id

    async def list_sessions(self, username: str, tenant_id: str = "default") -> List[Dict]:
        """List all sessions for a user with metadata"""
        if not self.redis: await self.connect()

        session_ids = await self.redis.lrange(self._get_user_sessions_key(tenant_id, username), 0, -1)
        sessions = []
        for sid in session_ids:
            meta_str = await self.redis.get(self._get_session_meta_key(sid))
            if meta_str:
                sessions.append(json.loads(meta_str))

        # Return reversed to show newest first
        return sessions[::-1]

    async def require_session_owner(
        self, username: str, session_id: str, tenant_id: str = "default"
    ) -> Dict:
        if not self.redis:
            await self.connect()

        meta_str = await self.redis.get(self._get_session_meta_key(session_id))
        if not meta_str:
            raise PermissionError("Session not found")

        meta = json.loads(meta_str)
        if meta.get("owner") != username or meta.get("tenant_id") != tenant_id:
            raise PermissionError("Session access denied")
        return meta

    async def add_message_to_session(
        self,
        username: str,
        session_id: str,
        message: Dict,
        limit: int = 100,
        tenant_id: str = "default",
    ):
        """Append a QA pair to a specific session"""
        if not self.redis: await self.connect()

        meta = await self.require_session_owner(username, session_id, tenant_id)

        key = self._get_session_messages_key(session_id)
        await self.redis.rpush(key, json.dumps(message))
        await self.redis.ltrim(key, -limit, -1)

        # Update session title if it's the first message
        meta_key = self._get_session_meta_key(session_id)
        if meta.get("title") == "New Chat" and "query" in message:
            # Use first 30 chars of query as title
            meta["title"] = (message["query"][:30] + "..") if len(message["query"]) > 30 else message["query"]
            await self.redis.set(meta_key, json.dumps(meta))

    async def get_session_messages(
        self, username: str, session_id: str, tenant_id: str = "default"
    ) -> List[Dict]:
        """Get all messages for a session"""
        if not self.redis: await self.connect()

        await self.require_session_owner(username, session_id, tenant_id)

        key = self._get_session_messages_key(session_id)
        data = await self.redis.lrange(key, 0, -1)
        return [json.loads(m) for m in data]

    # Legacy methods (kept for compatibility during transition)
    def _get_history_key(self, username: str) -> str:
        return f"history:{username}"

    async def get_chat_history(self, username: str, limit: int = 20) -> List[Dict]:
        """Legacy: Get flat history"""
        if not self.redis: await self.connect()
        key = self._get_history_key(username)
        data = await self.redis.lrange(key, -limit, -1)
        return [json.loads(m) for m in data]
