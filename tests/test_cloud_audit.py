import json

from src import config
from src.utils import cloud_audit


def test_cloud_audit_records_safe_metadata(tmp_path, monkeypatch):
    path = tmp_path / "cloud_calls.jsonl"
    monkeypatch.setattr(config, "CLOUD_AUDIT_PATH", str(path))
    monkeypatch.setattr(cloud_audit, "CLOUD_AUDIT_PATH", str(path))

    run_id = cloud_audit.record_cloud_call("agent", "model", ["query"])

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["run_id"] == run_id
    assert record["fields"] == ["query"]


def test_observable_model_call_marks_unavailable_telemetry_without_inventing_it():
    call = cloud_audit.observable_model_call(
        component="agent_b.predict",
        model="model-a",
        messages=[{"role": "user", "content": "question"}],
        response="answer",
        generation_config={"do_sample": False},
        latency_ms=1.0,
        status="succeeded",
    )

    assert call["usage"] == {
        "value": None,
        "unavailable_reason": "provider_usage_not_exposed",
    }
    assert call["token_ids"]["value"] is None
    assert call["model"]["tokenizer_sha256"]["unavailable_reason"]
