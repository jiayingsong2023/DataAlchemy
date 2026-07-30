import pytest

from src.ops.slo import summarize


def test_slo_summary_reports_latency_and_failure_rate():
    assert summarize([10, 20, 30, 40], 1) == {
        "count": 4.0,
        "p50_ms": 25.0,
        "p95_ms": 40,
        "error_rate": 0.2,
    }
    with pytest.raises(ValueError):
        summarize([], 0)
