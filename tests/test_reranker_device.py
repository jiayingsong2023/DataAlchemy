import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rag.retriever import Retriever


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
    assert vector_store.search_vector.call_args.kwargs["source_version"] == "sha256:fixture"
    assert vector_store.search_text.call_args.kwargs["source_version"] == "sha256:fixture"
