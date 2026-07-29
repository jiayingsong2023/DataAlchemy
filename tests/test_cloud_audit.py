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
