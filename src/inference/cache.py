import hashlib
import json
import time
from typing import Dict, List, Optional

import redis.asyncio as redis

from config import REDIS_URL
from utils.logger import logger

from .metrics import MetricsManager


class CacheManager:
    """
    Redis-backed TTL cache, session store, locks and queues only.
    """

    def __init__(
        self,
        redis_url: str = None,
        enable_semantic: bool | None = None,
        prefix: str = "dataalchemy",
    ):
        self.redis_url = redis_url or REDIS_URL
        self.redis: Optional[redis.Redis] = None
        self.prefix = prefix.rstrip(":")
        logger.info("CacheManager initialized (Redis: %s)", redis_url)

    async def connect(self):
        """Connect to Redis."""
        if self.redis is None:
            try:
                self.redis = redis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                logger.info("Connected to Redis")

            except Exception as e:
                logger.error(f"Redis connection failed: {e}")
                self.redis = None

    def _get_exact_key(self, prompt: str, kwargs: Dict, scope: str) -> str:
        """Create a unique key for exact match"""
        # Sort kwargs to ensure consistent hashing
        kwargs_str = json.dumps(kwargs, sort_keys=True)
        combined = f"{scope}||{prompt}||{kwargs_str}"
        return f"{self.prefix}:cache:exact:{hashlib.md5(combined.encode()).hexdigest()}"

    async def get(
        self, prompt: str, generation_kwargs: Dict, scope: Optional[str] = None
    ) -> Optional[str]:
        """Get an exact result from the scoped TTL cache."""
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

    async def clear(self):
        """Clear only DataAlchemy-owned cache and session keys."""
        if self.redis:
            keys = [key async for key in self.redis.scan_iter(f"{self.prefix}:*")]
            if keys:
                await self.redis.delete(*keys)
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
        if not self.redis:
            await self.connect()

        session_id = hashlib.md5(f"{tenant_id}:{username}:{time.time()}".encode()).hexdigest()[:12]

        # 1. Add to user's session list
        await self.redis.rpush(self._get_user_sessions_key(tenant_id, username), session_id)

        # 2. Store metadata
        meta = {
            "id": session_id,
            "owner": username,
            "tenant_id": tenant_id,
            "title": title,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        await self.redis.set(self._get_session_meta_key(session_id), json.dumps(meta))

        return session_id

    async def list_sessions(self, username: str, tenant_id: str = "default") -> List[Dict]:
        """List all sessions for a user with metadata"""
        if not self.redis:
            await self.connect()

        session_ids = await self.redis.lrange(
            self._get_user_sessions_key(tenant_id, username), 0, -1
        )
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
        if not self.redis:
            await self.connect()

        meta = await self.require_session_owner(username, session_id, tenant_id)

        key = self._get_session_messages_key(session_id)
        await self.redis.rpush(key, json.dumps(message))
        await self.redis.ltrim(key, -limit, -1)

        # Update session title if it's the first message
        meta_key = self._get_session_meta_key(session_id)
        if meta.get("title") == "New Chat" and "query" in message:
            # Use first 30 chars of query as title
            meta["title"] = (
                message["query"][:30] + ".." if len(message["query"]) > 30 else message["query"]
            )
            await self.redis.set(meta_key, json.dumps(meta))

    async def get_session_messages(
        self, username: str, session_id: str, tenant_id: str = "default"
    ) -> List[Dict]:
        """Get all messages for a session"""
        if not self.redis:
            await self.connect()

        await self.require_session_owner(username, session_id, tenant_id)

        key = self._get_session_messages_key(session_id)
        data = await self.redis.lrange(key, 0, -1)
        return [json.loads(m) for m in data]

    # Legacy methods (kept for compatibility during transition)
    def _get_history_key(self, username: str) -> str:
        return f"history:{username}"

    async def get_chat_history(self, username: str, limit: int = 20) -> List[Dict]:
        """Legacy: Get flat history"""
        if not self.redis:
            await self.connect()
        key = self._get_history_key(username)
        data = await self.redis.lrange(key, -limit, -1)
        return [json.loads(m) for m in data]
