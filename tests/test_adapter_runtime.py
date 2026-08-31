import pytest
import torch

from src.inference import adapter_runtime
from src.inference.adapter_runtime import AdapterRuntime, clean_model_response
from src.inference.model_manager import _decode_continuations


class _Tokenizer:
    def batch_decode(self, token_ids, *, skip_special_tokens):
        assert skip_special_tokens is True
        assert token_ids.tolist() == [[4, 5], [6, 7]]
        return ["first", "second"]


def test_model_manager_decodes_only_completion_tokens():
    output_ids = torch.tensor([[1, 2, 3, 4, 5], [1, 2, 3, 6, 7]])
    assert _decode_continuations(_Tokenizer(), output_ids, 3) == ["first", "second"]


def test_adapter_runtime_removes_protocol_leakage_and_rejects_invalid_unicode():
    assert clean_model_response("正确答案。### Instruction:\n下一题") == "正确答案。"
    assert clean_model_response("### Response:\n正确答案。") == "正确答案。"
    assert clean_model_response("正确\ufffd答案") == ""


class _Engine:
    async def generate(self, prompt, *, cache_scope, **kwargs):
        self.prompt = prompt
        self.cache_scope = cache_scope
        self.kwargs = kwargs
        return "正确答案。### Instruction:\n下一题"


@pytest.mark.asyncio
async def test_adapter_runtime_uses_deterministic_generation_and_sanitizes_output():
    agent = AdapterRuntime.__new__(AdapterRuntime)
    engine = _Engine()
    agent._ensure_engine = lambda identity: None
    agent.batch_engine = engine

    assert await agent.predict_async("问题", cache_scope="tenant:user") == "正确答案。"
    assert engine.kwargs["do_sample"] is False
    assert engine.kwargs["max_new_tokens"] == 128


def test_adapter_runtime_rechecks_adapter_scope_after_engine_started():
    agent = AdapterRuntime.__new__(AdapterRuntime)
    agent.batch_engine = object()
    calls = []
    agent.check_and_reload_adapter = lambda *, force, identity: calls.append((force, identity))

    agent._ensure_engine({"tenant_id": "tenant-b"})

    assert calls == [(False, {"tenant_id": "tenant-b"})]


def test_adapter_runtime_resolves_only_verified_promoted_tenant_adapter(monkeypatch):
    row = {
        "release_id": "release-1",
        "state": "verified",
        "adapter_id": "adapter-1",
    }
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, parameters):
            executed.append((query, parameters))

        def fetchone(self):
            return row

    class Transaction:
        def __enter__(self):
            return type("Connection", (), {"cursor": lambda _self: Cursor()})()

        def __exit__(self, *_args):
            return None

    class Database:
        def __init__(self, _url):
            pass

        def transaction(self, identity, *, read_only):
            assert identity["tenant_id"] == "acme"
            assert read_only is True
            return Transaction()

    monkeypatch.setenv("H5_LORA_MODE", "single_tenant_lora")
    monkeypatch.setenv("MODEL_RELEASE_TENANT_ID", "acme")
    monkeypatch.setattr(adapter_runtime, "PostgresDatabase", Database)

    resolved = AdapterRuntime.__new__(AdapterRuntime)._promoted_adapter({"tenant_id": "acme"})

    assert resolved == row
    assert "r.status = 'promoted'" in executed[0][0]
    assert "r.release_scope = 'single_tenant_lora'" in executed[0][0]
    assert executed[0][1] == ("acme",)
    row["state"] = "pending"
    assert AdapterRuntime.__new__(AdapterRuntime)._promoted_adapter({"tenant_id": "acme"}) is None


def test_adapter_runtime_rejects_adapter_artifact_hash_mismatch(tmp_path):
    class Store:
        @staticmethod
        def download_directory(_prefix, destination):
            (tmp_path / "adapter.staging" / "adapter.bin").write_bytes(b"weights")
            return destination

    agent = AdapterRuntime.__new__(AdapterRuntime)
    agent.adapter_path = str(tmp_path / "adapter")

    with pytest.raises(RuntimeError, match="adapter_artifact_hash_mismatch"):
        agent._download_exact_adapter(
            {
                "artifact_key": "adapters/adapter-1",
                "artifact_sha256": "0" * 64,
                "artifact_size": len(b"weights"),
            },
            Store(),
        )


def test_adapter_runtime_loads_promoted_adapter_and_reports_status(monkeypatch, tmp_path):
    row = {
        "release_id": "release-1",
        "adapter_id": "adapter-1",
        "artifact_sha256": "a" * 64,
        "base_model_digest": "b" * 64,
    }
    loads = []

    class Manager:
        base_model = None

        def load_models(self, **kwargs):
            loads.append(kwargs)
            self.base_model = object()

    agent = AdapterRuntime.__new__(AdapterRuntime)
    agent.model_id = "base-model"
    agent.model_manager = Manager()
    agent.last_sync_time = 0
    agent.last_release_id = None
    agent.last_adapter_id = None
    agent.last_artifact_sha256 = None
    agent._promoted_adapter = lambda _identity: row
    agent._download_exact_adapter = lambda _row, _store: str(tmp_path / "adapter.staging")
    monkeypatch.setattr(adapter_runtime, "S3Utils", object)

    assert agent.check_and_reload_adapter(
        identity={"tenant_id": "acme"}, expected_release_id="release-1"
    )
    assert loads == [
        {
            "base_model_id": "base-model",
            "lora_adapter_path": str(tmp_path / "adapter.staging"),
            "compile_model": True,
        }
    ]
    assert agent.model_status({"tenant_id": "acme"}) == {
        "tenant_id": "acme",
        "release_scope": "single_tenant_lora",
        "base_model_digest": "b" * 64,
        "adapter_id": "adapter-1",
        "adapter_artifact_sha256": "a" * 64,
        "release_id": "release-1",
        "loaded": True,
        "loaded_at": agent.loaded_at,
    }
