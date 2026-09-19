import pytest

from src.core import runtime_tool_handlers
from src.core.runtime_tools import register_runtime_tools
from src.core.tool_contracts import ToolRegistry
from src.rag.answering import LOCAL_ABSTENTION, GroundedAnswering


def document_evidence():
    return [
        {
            "document_id": "document-1",
            "chunk_id": "chunk-1",
            "context_type": "document",
            "text": "saved evidence",
        }
    ]


class AdapterRuntime:
    def __init__(self, calls):
        self.calls = calls

    async def predict_async(self, query, **kwargs):
        pytest.fail("chat must not generate unused adapter intuition")

    @staticmethod
    def model_status(identity):
        pytest.fail("chat must not load an unused adapter for status")


class Answering:
    # Explicit cloud-path stand-in: these tests exercise model-call capture and failure.
    client = object()
    model = "answer-model"

    def __init__(self, calls):
        self.calls = calls

    def respond(self, query, context, intuition, *, trace_recorder=None):
        assert context == document_evidence()
        assert intuition == ""
        self.calls.append(("respond", query))
        if trace_recorder:
            trace_recorder({"component": "agent_d.fusion", "status": "succeeded"})
        return {
            "answer": "answer",
            "citations": [
                {"document_id": "document-1", "chunk_id": "chunk-1", "quote": "saved evidence"}
            ],
            "answer_status": "answered",
            "answer_mode": "generated",
            "support_status": "not_semantically_verified",
            "reason_code": None,
        }


class Retriever:
    def __init__(self, calls):
        self.calls = calls

    def retrieve(self, query, identity, top_k):
        self.calls.append(("retrieve", query, identity, top_k))
        return document_evidence()


def register_tools(registry, calls, **kwargs):
    register_runtime_tools(
        registry,
        vector_store=object(),
        memory=object(),
        chat_adapter_runtime=AdapterRuntime(calls),
        chat_answering=Answering(calls),
        chat_retriever=Retriever(calls),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_runtime_capabilities_are_registered_as_tools():
    calls = []
    registry = ToolRegistry()
    register_tools(registry, calls)

    result = await registry.get("rag_chat").handler(
        {"query": "hello", "_identity": {"tenant_id": "acme", "username": "alice", "role": "user"}}
    )
    assert result["answer"] == "answer"
    assert result["answer_status"] == "answered"
    assert result["answer_mode"] == "generated"
    assert result["support_status"] == "not_semantically_verified"
    assert result["execution_status"] == "succeeded"
    assert result["model_execution"] == {
        "tenant_id": "acme",
        "generation": "used",
        "model_id": "answer-model",
    }
    assert calls[0] == (
        "retrieve",
        "hello",
        {"tenant_id": "acme", "username": "alice", "role": "user"},
        3,
    )
    assert [call[0] for call in calls] == ["retrieve", "respond"]
    assert registry.get("ingest").requires_approval
    assert registry.get("train").idempotent
    assert registry.get("release").roles == frozenset({"admin"})
    assert registry.get("sync_git").requires_approval
    assert registry.get("sync_git").uses_identity
    assert registry.get("ingest_document").requires_approval
    assert registry.get("ingest_document").uses_identity
    assert registry.get("h5_train_lora").job_kind == "lora_train"
    assert registry.get("h5_model_evaluate").job_kind == "model_evaluate"
    assert registry.get("h5_create_evaluation").requires_approval
    assert registry.get("h5_create_release_candidate").requires_approval
    assert registry.get("h5_observe_release").roles == frozenset({"admin"})


@pytest.mark.asyncio
async def test_rag_chat_consumes_the_saved_context_once():
    calls = []
    registry = ToolRegistry()
    loaded = []
    recorded = []

    def load(ref, digest):
        loaded.append((ref, digest))
        return {
            "query": "hello",
            "envelope_sha256": "c" * 64,
            "retrieval_context": document_evidence(),
        }

    def record(run_context, identity, envelope, result):
        recorded.append((run_context, identity, envelope, result))
        return {"response_ref": "response.json"}

    register_tools(
        registry,
        calls,
        chat_context_loader=load,
        chat_result_recorder=record,
    )
    result = await registry.get("rag_chat").handler(
        {
            "context_ref": "tenants/acme/context.json",
            "context_sha256": "a" * 64,
            "_identity": {"tenant_id": "acme", "username": "alice", "role": "user"},
            "_h3_context": {"task_id": "task-1"},
        }
    )

    assert result == {"response_ref": "response.json"}
    assert loaded == [("tenants/acme/context.json", "a" * 64)]
    assert [call[0] for call in calls] == ["respond"]
    assert recorded[0][2]["envelope_sha256"] == "c" * 64
    assert recorded[0][3]["query"] == "hello"
    assert recorded[0][3]["answer_status"] == "answered"
    assert recorded[0][3]["support_status"] == "not_semantically_verified"
    assert recorded[0][3]["model_calls"] == [{"component": "agent_d.fusion", "status": "succeeded"}]


@pytest.mark.asyncio
async def test_rag_chat_rejects_cross_tenant_context_before_loading():
    registry = ToolRegistry()
    loaded = []
    register_tools(
        registry,
        [],
        chat_context_loader=lambda *_args: loaded.append(True),
        chat_result_recorder=lambda *_args: {},
    )

    with pytest.raises(PermissionError, match="rag_chat_context_tenant_mismatch"):
        await registry.get("rag_chat").handler(
            {
                "context_ref": "tenants/other/context.json",
                "context_sha256": "a" * 64,
                "_identity": {"tenant_id": "acme", "username": "alice", "role": "user"},
            }
        )

    assert loaded == []


@pytest.mark.asyncio
async def test_rag_chat_records_failed_model_call_before_retrying():
    class BrokenAnswering(Answering):
        def respond(self, _query, context, _intuition, **kwargs):
            assert context == document_evidence()
            trace_recorder = kwargs["trace_recorder"]
            trace_recorder({"component": "agent_d.fusion", "status": "failed"})
            raise RuntimeError("model offline")

    recorded = []
    registry = ToolRegistry()
    calls = []
    register_runtime_tools(
        registry,
        vector_store=object(),
        memory=object(),
        chat_adapter_runtime=AdapterRuntime(calls),
        chat_answering=BrokenAnswering(calls),
        chat_retriever=Retriever(calls),
        chat_context_loader=lambda *_: {
            "query": "hello",
            "retrieval_context": document_evidence(),
        },
        chat_result_recorder=lambda *values: recorded.append(values) or {},
    )

    with pytest.raises(RuntimeError, match="model offline"):
        await registry.get("rag_chat").handler(
            {
                "context_ref": "tenants/acme/context.json",
                "context_sha256": "a" * 64,
                "_identity": {
                    "tenant_id": "acme",
                    "username": "alice",
                    "role": "user",
                },
                "_h3_context": {"task_id": "task-1", "attempt": 1},
            }
        )
    assert recorded[0][3]["status"] == "failed"
    assert recorded[0][3]["model_calls"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_rag_chat_empty_documents_abstains_without_cloud_or_adapter_calls():
    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = object()  # No usable cloud methods: calling it would fail this test.
    registry = ToolRegistry()
    recorded = []
    register_runtime_tools(
        registry,
        vector_store=object(),
        memory=object(),
        chat_adapter_runtime=AdapterRuntime([]),
        chat_answering=answering,
        chat_retriever=Retriever([]),
        chat_context_loader=lambda *_: {
            "query": "hello",
            "retrieval_context": [
                {"context_type": "memory", "text": "memory is not document evidence"}
            ],
        },
        chat_result_recorder=lambda *values: recorded.append(values) or {},
    )
    await registry.get("rag_chat").handler(
        {
            "context_ref": "tenants/acme/context.json",
            "context_sha256": "a" * 64,
            "_identity": {"tenant_id": "acme", "username": "alice", "role": "user"},
        }
    )
    result = recorded[0][3]
    assert result["answer"] == LOCAL_ABSTENTION
    assert result["answer_status"] == "abstained"
    assert result["reason_code"] == "no_document_evidence"
    assert result["support_status"] == "not_applicable"
    assert result["citations"] == result["model_calls"] == []
    assert result["model_execution"]["generation"] == "not_used"


def test_ingest_document_reads_only_the_raw_documents_prefix(monkeypatch):
    stored = []

    class ObjectStore:
        def get_object_body(self, key):
            assert key == "raw/documents/pilot.md"
            return b"# Pilot\n\nThe support window is Tuesday."

    class VectorStore:
        def add_documents(self, documents, identity, chunker):
            stored.extend(documents)
            assert identity["tenant_id"] == "acme"
            assert chunker is not None
            return ["document-1"]

    class Audit:
        def record(self, *_, **kwargs):
            assert kwargs["metadata"] == {"object_key": "raw/documents/pilot.md"}

    monkeypatch.setattr(runtime_tool_handlers, "S3Utils", lambda: ObjectStore())
    monkeypatch.setattr(runtime_tool_handlers, "AuditLog", lambda _: Audit())

    result = runtime_tool_handlers._ingest_document(
        VectorStore(),
        {
            "object_key": "raw/documents/pilot.md",
            "_identity": {"tenant_id": "acme", "username": "alice", "role": "admin"},
        },
    )
    assert result["document_id"] == "document-1"
    assert stored[0]["metadata"]["raw_object_key"] == "raw/documents/pilot.md"
