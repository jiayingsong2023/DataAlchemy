import functools
import time

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, Summary


def _metric(metric_type, name, documentation, *args, **kwargs):
    """Reuse a default-registry collector when legacy import paths load this module twice."""
    return REGISTRY._names_to_collectors.get(name) or metric_type(
        name, documentation, *args, **kwargs
    )


# Metrics definitions
INFERENCE_LATENCY = _metric(
    Histogram,
    "inference_latency_seconds",
    "Time spent processing an inference request",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, float("inf")),
)

BATCH_SIZE = _metric(
    Histogram,
    "inference_batch_size",
    "Number of requests in a single batch",
    buckets=(1, 2, 4, 8, 16, 32),
)

CACHE_HITS = _metric(
    Counter,
    "inference_cache_hits_total",
    "Total number of cache hits",
    ["type"],  # 'exact' or 'semantic'
)

CACHE_MISSES = _metric(Counter, "inference_cache_misses_total", "Total number of cache misses")

GPU_MEMORY_USAGE = _metric(
    Gauge, "gpu_memory_usage_bytes", "Current GPU memory usage in bytes", ["device"]
)

MODEL_LOAD_TIME = _metric(Summary, "model_load_seconds", "Time taken to load models")

ACTIVE_REQUESTS = _metric(
    Gauge, "inference_active_requests", "Number of currently active inference requests"
)


def track_latency(func):
    """Decorator to track function latency"""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            return await func(*args, **kwargs)
        finally:
            INFERENCE_LATENCY.observe(time.time() - start_time)

    return wrapper


class MetricsManager:
    """Helper to update complex metrics"""

    @staticmethod
    def record_cache_hit(hit_type: str):
        CACHE_HITS.labels(type=hit_type).inc()

    @staticmethod
    def record_cache_miss():
        CACHE_MISSES.inc()

    @staticmethod
    def record_batch_size(size: int):
        BATCH_SIZE.observe(size)

    @staticmethod
    def update_gpu_memory(device_id: int, usage_bytes: int):
        GPU_MEMORY_USAGE.labels(device=f"cuda:{device_id}").set(usage_bytes)

    @staticmethod
    def set_active_requests(count: int):
        ACTIVE_REQUESTS.set(count)
