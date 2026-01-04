import json
import hashlib
import redis.asyncio as redis
from typing import Optional, Dict, Any, List
import numpy as np

# Walkaround for torch.distributed compatibility issues (e.g. on Windows or specific ROCm builds)
# sentence-transformers checks for torch.distributed.is_initialized()
import torch
if not hasattr(torch, 'distributed'):
    import types
    torch.distributed = types.ModuleType("torch.distributed")
if not hasattr(torch.distributed, 'is_initialized'):
    torch.distributed.is_initialized = lambda: False

from sentence_transformers import SentenceTransformer
import time
from .metrics import MetricsManager

class CacheManager:
    """
    Advanced Cache Manager supporting Redis persistence and Semantic Search.
    """
    
    def __init__(
        self, 
        redis_url: str = "redis://localhost:6379",
        enable_semantic: bool = True,
        semantic_threshold: float = 0.92,
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    ):
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self.enable_semantic = enable_semantic
        self.semantic_threshold = semantic_threshold
        
        # Lazy load embedding model
        self.embedding_model_name = embedding_model
        self.model = None
        
        # Local semantic index (for simple vector search)
        self.semantic_index: List[Dict[str, Any]] = [] 
        self.semantic_redis_key = "cache:semantic:index"
        
        print(f"[CacheManager] Initialized (Redis: {redis_url}, Semantic: {enable_semantic})")

    async def connect(self):
        """Connect to Redis and load semantic index"""
        if self.redis is None:
            try:
                self.redis = redis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                print("[CacheManager] Connected to Redis")
                
                # Load semantic index from Redis
                if self.enable_semantic:
                    await self._load_semantic_index()
            except Exception as e:
                print(f"[CacheManager] Redis connection failed: {e}")
                self.redis = None

    def _get_exact_key(self, prompt: str, kwargs: Dict) -> str:
        """Create a unique key for exact match"""
        # Sort kwargs to ensure consistent hashing
        kwargs_str = json.dumps(kwargs, sort_keys=True)
        combined = f"{prompt}||{kwargs_str}"
        return f"cache:exact:{hashlib.md5(combined.encode()).hexdigest()}"

    async def get(self, prompt: str, generation_kwargs: Dict) -> Optional[str]:
        """Get result from cache (Exact -> Semantic)"""
        if self.redis is None:
            await self.connect()
            
        if self.redis is None:
            return None

        # 1. Try Exact Match
        exact_key = self._get_exact_key(prompt, generation_kwargs)
        cached = await self.redis.get(exact_key)
        if cached:
            print(f"[CacheManager] Exact match hit!")
            MetricsManager.record_cache_hit("exact")
            return cached

        # 2. Try Semantic Match (if enabled)
        if self.enable_semantic:
            res = await self._get_semantic(prompt)
            if res:
                MetricsManager.record_cache_hit("semantic")
            return res

        return None

    async def set(self, prompt: str, generation_kwargs: Dict, result: str):
        """Store result in cache"""
        if self.redis is None:
            await self.connect()
            
        if self.redis is None:
            return

        # 1. Store Exact Match (TTL: 24 hours)
        exact_key = self._get_exact_key(prompt, generation_kwargs)
        await self.redis.setex(exact_key, 86400, result)

        # 2. Update Semantic Index
        if self.enable_semantic:
            await self._add_semantic(prompt, result)

    async def _get_semantic(self, prompt: str) -> Optional[str]:
        """Simple semantic search implementation"""
        if not self.semantic_index:
            return None
            
        if self.model is None:
            print(f"[CacheManager] Loading embedding model: {self.embedding_model_name}")
            self.model = SentenceTransformer(self.embedding_model_name)

        # Generate embedding for query
        query_vec = self.model.encode([prompt])[0]
        
        best_score = -1
        best_result = None
        
        # Simple cosine similarity search
        for item in self.semantic_index:
            score = np.dot(query_vec, item["vector"]) / (
                np.linalg.norm(query_vec) * np.linalg.norm(item["vector"])
            )
            if score > best_score:
                best_score = score
                best_result = item["result"]
        
        if best_score >= self.semantic_threshold:
            print(f"[CacheManager] Semantic hit! (Score: {best_score:.4f})")
            return best_result
            
        return None

    async def _add_semantic(self, prompt: str, result: str):
        """Add entry to semantic index and persist to Redis"""
        if self.model is None:
            self.model = SentenceTransformer(self.embedding_model_name)
            
        vector = self.model.encode([prompt])[0]
        entry = {
            "vector": vector.tolist(), # Convert to list for JSON serialization
            "result": result,
            "prompt": prompt
        }
        self.semantic_index.append({
            "vector": vector, # Keep as numpy for local search
            "result": result,
            "prompt": prompt
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
            
        print("[CacheManager] Loading semantic index from Redis...")
        data = await self.redis.lrange(self.semantic_redis_key, 0, -1)
        self.semantic_index = []
        for item_str in data:
            item = json.loads(item_str)
            self.semantic_index.append({
                "vector": np.array(item["vector"]),
                "result": item["result"],
                "prompt": item["prompt"]
            })
        print(f"[CacheManager] Loaded {len(self.semantic_index)} semantic entries")

    async def clear(self):
        """Clear all caches"""
        if self.redis:
            await self.redis.flushdb()
        self.semantic_index = []
        print("[CacheManager] Cache cleared")

    # --- Session & History Management ---

    def _get_history_key(self, username: str) -> str:
        return f"history:{username}"

    def _get_session_key(self, session_id: str) -> str:
        return f"session:{session_id}"

    async def save_session(self, session_id: str, data: Dict, ttl: int = 3600):
        """Save session data to Redis (default TTL: 1 hour)"""
        if self.redis:
            await self.redis.setex(self._get_session_key(session_id), ttl, json.dumps(data))

    async def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session data from Redis"""
        if self.redis:
            data = await self.redis.get(self._get_session_key(session_id))
            return json.loads(data) if data else None
        return None

    async def add_chat_history(self, username: str, message: Dict, limit: int = 50):
        """Append a message to user's chat history"""
        if self.redis:
            key = self._get_history_key(username)
            await self.redis.rpush(key, json.dumps(message))
            await self.redis.ltrim(key, -limit, -1)

    async def get_chat_history(self, username: str, limit: int = 20) -> List[Dict]:
        """Get the last N messages for a user"""
        if self.redis:
            key = self._get_history_key(username)
            data = await self.redis.lrange(key, -limit, -1)
            return [json.loads(m) for m in data]
        return []
