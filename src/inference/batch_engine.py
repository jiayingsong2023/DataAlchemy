"""
Async Batch Inference Engine with dynamic batching and caching
Optimized for AMD AI Max+ 395
"""
import asyncio
import time
from collections import deque, OrderedDict
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
import hashlib

from .model_manager import ModelManager


@dataclass
class InferenceRequest:
    """Single inference request"""
    prompt: str
    future: asyncio.Future
    timestamp: float
    generation_kwargs: Dict[str, Any]


class LRUCache:
    """Simple LRU cache for inference results"""
    
    def __init__(self, max_size: int = 1000):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
    
    def _hash_key(self, prompt: str, kwargs: Dict) -> str:
        """Create cache key from prompt and generation kwargs"""
        key_str = f"{prompt}:{sorted(kwargs.items())}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, prompt: str, kwargs: Dict) -> Optional[str]:
        """Get cached result"""
        key = self._hash_key(prompt, kwargs)
        if key in self.cache:
            self.hits += 1
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]
        self.misses += 1
        return None
    
    def put(self, prompt: str, kwargs: Dict, result: str):
        """Store result in cache"""
        key = self._hash_key(prompt, kwargs)
        self.cache[key] = result
        self.cache.move_to_end(key)
        
        # Evict oldest if over capacity
        if len(self.cache) > self.max_size:
            self.cache.popitem(last=False)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        total = self.hits + self.misses
        hit_rate = self.hits / total if total > 0 else 0
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": hit_rate
        }
    
    def clear(self):
        """Clear cache"""
        self.cache.clear()
        self.hits = 0
        self.misses = 0


class BatchInferenceEngine:
    """
    Async batch inference engine with dynamic batching
    
    Features:
    - Dynamic batching: accumulate requests up to max_batch_size or max_wait_ms
    - LRU caching: cache results for repeated queries
    - Async API: non-blocking request handling
    """
    
    def __init__(
        self,
        model_manager: ModelManager,
        max_batch_size: int = 8,
        max_wait_ms: int = 50,
        cache_size: int = 1000,
        enable_cache: bool = True
    ):
        """
        Initialize batch inference engine
        
        Args:
            model_manager: ModelManager instance
            max_batch_size: Maximum batch size for inference
            max_wait_ms: Maximum wait time (ms) before processing batch
            cache_size: Maximum cache size
            enable_cache: Whether to enable caching
        """
        self.model_manager = model_manager
        self.max_batch_size = max_batch_size
        self.max_wait_ms = max_wait_ms / 1000.0  # Convert to seconds
        
        self.queue: deque[InferenceRequest] = deque()
        self.processing = False
        self.enable_cache = enable_cache
        self.cache = LRUCache(max_size=cache_size) if enable_cache else None
        
        # Statistics
        self.total_requests = 0
        self.total_batches = 0
        self.total_cache_hits = 0
        
        # Start background batch processor
        self._processor_task = None
        
        print(f"[BatchEngine] Initialized (batch_size={max_batch_size}, wait_ms={max_wait_ms*1000})")
    
    async def generate(self, prompt: str, **generation_kwargs) -> str:
        """
        Generate text for a single prompt (async)
        
        Args:
            prompt: Input prompt
            **generation_kwargs: Generation parameters
        
        Returns:
            Generated text
        """
        self.total_requests += 1
        
        # Check cache first
        if self.enable_cache:
            cached_result = self.cache.get(prompt, generation_kwargs)
            if cached_result is not None:
                self.total_cache_hits += 1
                return cached_result
        
        # Create request and add to queue
        future = asyncio.Future()
        request = InferenceRequest(
            prompt=prompt,
            future=future,
            timestamp=time.time(),
            generation_kwargs=generation_kwargs
        )
        
        self.queue.append(request)
        
        # Start processor if not running
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_queue())
        
        # Wait for result
        result = await future
        
        # Cache result
        if self.enable_cache:
            self.cache.put(prompt, generation_kwargs, result)
        
        return result
    
    async def _process_queue(self):
        """Background task to process batches"""
        while self.queue:
            # Wait for batch to fill or timeout
            batch_start = time.time()
            
            while len(self.queue) < self.max_batch_size:
                elapsed = time.time() - batch_start
                if elapsed >= self.max_wait_ms:
                    break
                
                # Small sleep to avoid busy waiting
                await asyncio.sleep(0.001)
                
                # If queue is empty, exit
                if not self.queue:
                    return
            
            # Process batch
            await self._process_batch()
    
    async def _process_batch(self):
        """Process a single batch of requests"""
        if not self.queue:
            return
        
        # Extract batch
        batch_size = min(len(self.queue), self.max_batch_size)
        batch = [self.queue.popleft() for _ in range(batch_size)]
        
        self.total_batches += 1
        
        # Group by generation kwargs (for efficiency)
        # For simplicity, we'll process all together for now
        prompts = [req.prompt for req in batch]
        
        # Use first request's kwargs as default (can be improved)
        generation_kwargs = batch[0].generation_kwargs
        
        try:
            # Run inference in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                self.model_manager.generate,
                prompts,
                generation_kwargs  # Pass as dict, not **kwargs
            )
            
            # Set results
            for req, result in zip(batch, results):
                if not req.future.done():
                    req.future.set_result(result)
                    
        except Exception as e:
            # Set exception for all requests
            for req in batch:
                if not req.future.done():
                    req.future.set_exception(e)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics"""
        stats = {
            "total_requests": self.total_requests,
            "total_batches": self.total_batches,
            "avg_batch_size": self.total_requests / self.total_batches if self.total_batches > 0 else 0,
            "queue_size": len(self.queue),
            "cache_hits": self.total_cache_hits,
            "cache_hit_rate": self.total_cache_hits / self.total_requests if self.total_requests > 0 else 0,
        }
        
        if self.enable_cache:
            stats["cache"] = self.cache.get_stats()
        
        return stats
    
    def clear_cache(self):
        """Clear inference cache"""
        if self.cache:
            self.cache.clear()
            print("[BatchEngine] Cache cleared")
    
    async def shutdown(self):
        """Shutdown the batch engine and cleanup resources"""
        print("[BatchEngine] Shutting down...")
        
        # Cancel processor task if running
        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        # Clear queue
        while self.queue:
            req = self.queue.popleft()
            if not req.future.done():
                req.future.cancel()
        
        print("[BatchEngine] Shutdown complete")
