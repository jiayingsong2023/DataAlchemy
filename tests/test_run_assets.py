import json

import pytest

from src.storage.run_assets import publish_run


def test_publish_is_atomic_and_hash_verified(tmp_path):
    target = publish_run(tmp_path, "run-1", {"input": "v1", "state": "succeeded"})
    assert json.loads(target.read_text())["state"] == "succeeded"
    assert (tmp_path / "current").read_text() == "run-1"
    with pytest.raises(FileExistsError):
        publish_run(tmp_path, "run-1", {})
