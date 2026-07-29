import json

import pytest

from scripts.evaluate_phase0_baseline import main


def test_baseline_rejects_missing_tasks(tmp_path, monkeypatch):
    results = tmp_path / "results.json"
    results.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["evaluate", str(results)])

    with pytest.raises(SystemExit, match="baseline incomplete"):
        main()
