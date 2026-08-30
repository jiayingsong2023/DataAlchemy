from prometheus_client import generate_latest

from inference.metrics import LEGACY_AGENT_CALLS  # noqa: F401


def test_legacy_agent_metrics_publish_explicit_zero_series():
    metrics = generate_latest().decode()

    for entrypoint in ("chat_async", "chat_with_citations_async"):
        for route in ("direct", "runtime_adapter"):
            assert (
                f'legacy_agent_calls_total{{entrypoint="{entrypoint}",route="{route}"}} 0.0'
                in metrics
            )
