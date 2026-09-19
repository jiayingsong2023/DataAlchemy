from types import SimpleNamespace

import pytest

from src.harness.rtd_q4_http import _post
from src.harness.rtd_q4_performance import _percentile, _quality_passed, _score, _summary


@pytest.mark.asyncio
async def test_local_performance_separates_answering_from_unused_generation():
    from src.harness.rtd_q4_performance import _request
    from src.rag.answering import GroundedAnswering

    answering = GroundedAnswering.__new__(GroundedAnswering)
    answering.client = None

    def retrieve(*_args, **kwargs):
        kwargs["timings"].update(
            dict.fromkeys(["embedding_ms", "vector_ms", "fts_ms", "fusion_ms", "reranker_ms"], 0.0)
        )
        return []

    retriever = SimpleNamespace(retrieve=retrieve)
    result = await _request(
        "candidate",
        {"case_id": "empty", "query": "question", "required_substrings": [], "required_pages": []},
        [],
        {"tenant_id": "test", "username": "alice", "role": "user"},
        retriever,
        object(),
        answering,
    )
    assert result["timings"]["generation_ms"] == 0
    assert result["timings"]["answering_ms"] >= 0
    assert result["model_execution"]["generation"] == "not_used"


def test_performance_helpers_preserve_tail_and_citation_gate():
    assert _percentile([1.0, 2.0, 3.0, 100.0], 95) == 100.0
    assert _summary([1.0, 2.0, 3.0, 100.0])["p99"] == 100.0
    case = {"required_substrings": ["answer"], "required_pages": [2]}
    citation = {
        "locator": {"page": 2},
        "source_span_ids": ["span-1"],
        "source_content_sha256": "a" * 64,
        "acl_digest": "b" * 64,
    }
    assert _score(case, "the answer", [citation])
    assert not _score(case, "the answer", [{**citation, "acl_digest": None}])


def test_candidate_quality_is_no_regression_not_baseline_perfection():
    assert _quality_passed({"passed": 0, "requests": 21}, {"passed": 21, "requests": 21})
    assert not _quality_passed({"passed": 21, "requests": 21}, {"passed": 20, "requests": 21})


def test_http_runner_posts_to_governed_chat_with_ingress_host(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"answer":"ok"}'

    seen = {}

    def open_request(request, timeout):
        seen.update(url=request.full_url, host=request.headers["Host"], timeout=timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    assert _post("http://traefik", "data-alchemy.test", "token", "query") == {"answer": "ok"}
    assert seen == {
        "url": "http://traefik/api/chat",
        "host": "data-alchemy.test",
        "timeout": 300,
    }
