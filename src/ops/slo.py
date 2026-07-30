"""Small, dependency-free SLO summary used to baseline release candidates."""

from __future__ import annotations

from statistics import median


def summarize(latencies_ms: list[float], failures: int) -> dict[str, float]:
    if not latencies_ms or failures < 0:
        raise ValueError("latencies must be non-empty and failures non-negative")
    ordered = sorted(latencies_ms)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))
    return {
        "count": float(len(ordered)),
        "p50_ms": median(ordered),
        "p95_ms": ordered[index],
        "error_rate": failures / (len(ordered) + failures),
    }
