import pytest
import torch

from src.agents.agent_b import AgentB, _clean_model_response
from src.inference.model_manager import _decode_continuations


class _Tokenizer:
    def batch_decode(self, token_ids, *, skip_special_tokens):
        assert skip_special_tokens is True
        assert token_ids.tolist() == [[4, 5], [6, 7]]
        return ["first", "second"]


def test_model_manager_decodes_only_completion_tokens():
    output_ids = torch.tensor([[1, 2, 3, 4, 5], [1, 2, 3, 6, 7]])
    assert _decode_continuations(_Tokenizer(), output_ids, 3) == ["first", "second"]


def test_agent_b_removes_protocol_leakage_and_rejects_invalid_unicode():
    assert _clean_model_response("正确答案。### Instruction:\n下一题") == "正确答案。"
    assert _clean_model_response("### Response:\n正确答案。") == "正确答案。"
    assert _clean_model_response("正确\ufffd答案") == ""


class _Engine:
    async def generate(self, prompt, *, cache_scope, **kwargs):
        self.prompt = prompt
        self.cache_scope = cache_scope
        self.kwargs = kwargs
        return "正确答案。### Instruction:\n下一题"


@pytest.mark.asyncio
async def test_agent_b_uses_deterministic_generation_and_sanitizes_output():
    agent = AgentB.__new__(AgentB)
    engine = _Engine()
    agent._ensure_engine = lambda identity: None
    agent.batch_engine = engine

    assert await agent.predict_async("问题", cache_scope="tenant:user") == "正确答案。"
    assert engine.kwargs["do_sample"] is False
    assert engine.kwargs["max_new_tokens"] == 128


def test_agent_b_rechecks_adapter_scope_after_engine_started():
    agent = AgentB.__new__(AgentB)
    agent.batch_engine = object()
    calls = []
    agent.check_and_reload_adapter = lambda *, force, identity: calls.append((force, identity))

    agent._ensure_engine({"tenant_id": "tenant-b"})

    assert calls == [(False, {"tenant_id": "tenant-b"})]
