import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rag.retriever import Retriever
from rag.vector_store import VectorStore


def test_reranker_uses_inference_service():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = [
        {"chunk_id": "one", "text": "one", "source": "test"},
        {"chunk_id": "two", "text": "two", "source": "test"},
    ]
    vector_store.search_text.return_value = []
    vector_store.inference.rerank.return_value = [0.9, 0.1]
    retriever = Retriever(vector_store)
    retriever.retrieve(
        "question",
        {"tenant_id": "test", "username": "test", "role": "user"},
        top_k=1,
        source_version="sha256:fixture",
    )

    vector_store.inference.rerank.assert_called_once_with("question", ["one", "two"])
    assert vector_store.search_vector.call_args.kwargs["top_k"] == 20
    assert vector_store.search_vector.call_args.kwargs["source_version"] == "sha256:fixture"
    assert vector_store.search_text.call_args.kwargs["source_version"] == "sha256:fixture"


def test_retriever_accepts_legacy_cpu_thread_budget(monkeypatch):
    monkeypatch.setenv("RAG_CPU_THREADS", "2")
    vector_store = MagicMock()
    Retriever(vector_store)


def test_retrieval_overfetches_for_reranking():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = [{"chunk_id": "one", "text": "one"}]
    vector_store.search_text.return_value = []

    Retriever(vector_store).retrieve("question", {"tenant_id": "test"}, top_k=5)

    assert vector_store.search_vector.call_args.kwargs["top_k"] == 100
    assert vector_store.search_text.call_args.kwargs["top_k"] == 100


def test_reranking_is_limited_after_full_first_stage_recall():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = [
        {"chunk_id": f"vector-{index}", "text": "text"} for index in range(100)
    ]
    vector_store.search_text.return_value = [
        {"chunk_id": f"text-{index}", "text": "text"} for index in range(100)
    ]
    vector_store.inference.rerank.return_value = [0.0] * 20
    Retriever(vector_store).retrieve("question", {"tenant_id": "test"}, top_k=5)

    assert len(vector_store.inference.rerank.call_args.args[1]) == 20
    assert vector_store.search_vector.call_args.kwargs["top_k"] == 100
    assert vector_store.search_text.call_args.kwargs["top_k"] == 100


def test_retrieval_forwards_explicit_document_scope():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = []
    vector_store.search_text.return_value = []

    Retriever(vector_store).retrieve(
        "question", {"tenant_id": "test"}, document_ids=["00000000-0000-0000-0000-000000000001"]
    )

    expected = ["00000000-0000-0000-0000-000000000001"]
    assert vector_store.search_vector.call_args.kwargs["document_ids"] == expected
    assert vector_store.search_text.call_args.kwargs["document_ids"] == expected


def test_retrieval_forwards_governed_lineage_scope():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = []
    vector_store.search_text.return_value = []

    Retriever(vector_store).retrieve("question", {"tenant_id": "test"}, governed_only=True)

    assert vector_store.search_vector.call_args.kwargs["governed_only"] is True
    assert vector_store.search_text.call_args.kwargs["governed_only"] is True


def test_vector_store_document_scope_is_fail_closed():
    vector_store = VectorStore(model_name="embedding")
    vector_store.model = MagicMock()
    vector_store.model.encode.return_value = [[0.5, 0.25]]
    vector_store._search = MagicMock(return_value=[])
    document_ids = ["00000000-0000-0000-0000-000000000001"]

    vector_store.search_vector("question", {"tenant_id": "test"}, document_ids=document_ids)
    vector_store.search_text("question", {"tenant_id": "test"}, document_ids=document_ids)

    for call in vector_store._search.call_args_list:
        assert "d.document_id = ANY(%s::uuid[])" in call.args[1]
        assert document_ids in call.args[2]
    vector_store._search.reset_mock()
    assert vector_store.search_vector("question", {"tenant_id": "test"}, document_ids=[]) == []
    assert vector_store.search_text("question", {"tenant_id": "test"}, document_ids=[]) == []
    vector_store._search.assert_not_called()


def test_vector_store_governed_scope_requires_complete_lineage():
    vector_store = VectorStore(model_name="embedding")
    vector_store.model = MagicMock()
    vector_store.model.encode.return_value = [[0.5, 0.25]]
    vector_store._search = MagicMock(return_value=[])

    vector_store.search_vector("question", {"tenant_id": "test"}, governed_only=True)
    vector_store.search_text("question", {"tenant_id": "test"}, governed_only=True)

    for call in vector_store._search.call_args_list:
        query = call.args[1]
        assert "source_content_sha256" in query
        assert "acl_digest" in query
        assert "jsonb_array_length(c.metadata_json->'source_span_ids') > 0" in query


def test_vector_store_uses_inference_service():
    inference = MagicMock()
    inference.embed.return_value = [[0.5, 0.25]]
    store = VectorStore(model_name="embedding", inference=inference)

    assert store.encode(["question"]) == [[0.5, 0.25]]
    inference.embed.assert_called_once_with(["question"])


def test_retrieval_records_stage_timings():
    vector_store = VectorStore(model_name="embedding")
    vector_store.model = MagicMock()
    vector_store.model.encode.return_value = [[0.5, 0.25]]
    vector_store._search = MagicMock(
        side_effect=[
            [{"chunk_id": "one", "text": "one"}],
            [{"chunk_id": "one", "text": "one"}],
        ]
    )
    timings = {}

    Retriever(vector_store).retrieve(
        "question", {"tenant_id": "test"}, rerank=False, timings=timings
    )

    assert set(timings) == {"embedding_ms", "vector_ms", "fts_ms", "fusion_ms", "reranker_ms"}
    assert all(value >= 0 for value in timings.values())
