import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rag.retriever import Retriever
from rag.vector_store import VectorStore


def test_reranker_defaults_to_cpu(monkeypatch):
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)
    vector_store = MagicMock()
    vector_store.search_vector.return_value = [
        {"chunk_id": "one", "text": "one", "source": "test"},
        {"chunk_id": "two", "text": "two", "source": "test"},
    ]
    vector_store.search_text.return_value = []
    retriever = Retriever(vector_store)
    with patch("rag.retriever._load_cross_encoder") as cross_encoder:
        cross_encoder.return_value.predict.return_value = [0.9, 0.1]
        retriever.retrieve(
            "question",
            {"tenant_id": "test", "username": "test", "role": "user"},
            top_k=1,
            source_version="sha256:fixture",
        )

    assert cross_encoder.call_args.args[1] == "cpu"
    assert vector_store.search_vector.call_args.kwargs["top_k"] == 20
    assert vector_store.search_vector.call_args.kwargs["source_version"] == "sha256:fixture"
    assert vector_store.search_text.call_args.kwargs["source_version"] == "sha256:fixture"


def test_retrieval_overfetches_for_reranking():
    vector_store = MagicMock()
    vector_store.search_vector.return_value = [{"chunk_id": "one", "text": "one"}]
    vector_store.search_text.return_value = []

    Retriever(vector_store).retrieve("question", {"tenant_id": "test"}, top_k=5)

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


def test_vector_store_defaults_to_cpu(monkeypatch):
    monkeypatch.delenv("EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    with patch("rag.vector_store._load_sentence_transformer") as loader:
        VectorStore(model_name="embedding")._load_model()

    assert loader.call_count == 1
    assert loader.call_args.kwargs == {"device": "cpu", "local_files_only": True}
