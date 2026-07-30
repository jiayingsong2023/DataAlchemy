import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rag.retriever import Retriever


def test_reranker_defaults_to_cpu(monkeypatch):
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)
    vector_store = MagicMock()
    vector_store.search.return_value = [
        {"text": "one", "source": "test"},
        {"text": "two", "source": "test"},
    ]
    retriever = Retriever(vector_store)
    retriever._init_bm25 = MagicMock()

    with patch("rag.retriever.CrossEncoder") as cross_encoder:
        cross_encoder.return_value.predict.return_value = [0.9, 0.1]
        retriever.retrieve("question", top_k=1)

    assert cross_encoder.call_args.kwargs["device"] == "cpu"
