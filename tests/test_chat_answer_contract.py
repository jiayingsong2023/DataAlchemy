"""Deterministic contract checks, not business accuracy or judge calibration."""

import json
from types import SimpleNamespace

import pytest

from core.agent_runtime import AgentRuntime
from core.tool_contracts import ToolSpec
from rag.answering import LOCAL_ABSTENTION, GroundedAnswering, _parse_generated_answer


def document(text, chunk="chunk"):
    return {"context_type": "document", "document_id": "doc", "chunk_id": chunk, "text": text}


def local(query, rows):
    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = None
    return answering.respond(query, rows, "untrusted intuition")


@pytest.mark.parametrize(
    "query,text",
    [
        ("What is Orion's timeout?", "Orion timeout is 30 seconds."),
        ("星河的超时是多少？", "星河的超时为30秒。"),
        ("星河的超时是多少？", "星河的超\n时为30秒。"),
    ],
)
def test_only_selected_verbatim_chunk_is_cited(query, text):
    result = local(query, [document("Unrelated source.", "other"), document(text)])
    assert result["answer_status"] == "answered"
    assert result["support_status"] == "extract_verified"
    assert len(result["citations"]) == 1
    citation = result["citations"][0]
    assert citation["chunk_id"] == "chunk"
    assert text[citation["quote_start"] : citation["quote_end"]] == citation["quote"]
    assert result["answer"] == "根据文档：" + citation["quote"]


@pytest.mark.parametrize(
    "text", ["Orion doesn't use HTTP.", "Orion doesn’t use HTTP.", "Orion does not use HTTP."]
)
def test_unresolved_negation_abstains(text):
    result = local("Does Orion use HTTP?", [document(text)])
    assert result["answer_status"] == "abstained"
    assert result["citations"] == []


def test_conflicting_sources_and_wrong_entity_abstain():
    conflict = local(
        "Orion timeout?",
        [
            document("Orion timeout is 30 seconds."),
            document("Orion timeout is 60 seconds.", "other"),
        ],
    )
    assert conflict["reason_code"] == "conflicting_evidence"
    wrong = local("Alice title?", [document("Bob title is captain.")])
    assert wrong["answer"] == LOCAL_ABSTENTION
    assert wrong["citations"] == []


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [document(" \n ")],
        [{"context_type": "memory", "text": "Fact", "chunk_id": "m", "document_id": "d"}],
    ],
)
def test_cloud_no_document_evidence_never_touches_client(rows):
    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = object()  # Any client access would fail.
    assert (
        answering.respond("question", rows, "plausible answer")["reason_code"]
        == "no_document_evidence"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"citations": []},
        {"citations": [{"chunk_id": "fake", "quote": "Orion timeout is 30."}]},
        {"citations": [{"chunk_id": "chunk", "quote": "invented"}]},
        {"answer_status": "failed"},
    ],
)
def test_generated_citation_or_schema_failure_is_not_an_answer(changes):
    value = {
        "answer": "30",
        "answer_status": "answered",
        "citations": [{"chunk_id": "chunk", "quote": "Orion timeout is 30."}],
    }
    with pytest.raises(ValueError):
        _parse_generated_answer(
            json.dumps({**value, **changes}), [document("Orion timeout is 30.")]
        )


@pytest.mark.asyncio
async def test_v1_chat_task_cannot_execute_new_tool_through_generic_runtime():
    runtime = AgentRuntime.__new__(AgentRuntime)

    def forbidden(*_args):
        pytest.fail("legacy chat must be rejected before reserve or execute")

    runtime._reserve_tool_run = forbidden
    task = {"task_spec": {"success_criteria": [{"verifier": "verify_chat_capture", "version": 1}]}}
    spec = ToolSpec(name="rag_chat", handler=forbidden, schema={})
    with pytest.raises(ValueError, match="v1_read_only"):
        await runtime._call_tool(task, spec, {}, {})


def test_cloud_exception_propagates_and_retains_failed_trace(monkeypatch):
    def fail(**_kwargs):
        raise ConnectionError("provider unavailable")

    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fail))
    )
    answering.model, answering.temperature, answering.max_tokens = "test", 0, 64
    monkeypatch.setattr("rag.answering.sanitize_for_cloud", lambda value: value)
    monkeypatch.setattr("rag.answering.record_cloud_call", lambda *_args: None)
    traces = []
    with pytest.raises(ConnectionError):
        answering.respond("Orion timeout?", [document("Orion timeout is 30.")], "", traces.append)
    assert len(traces) == 1 and traces[0]["status"] == "failed"
