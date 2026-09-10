from fastapi.testclient import TestClient

from src.inference import service


class Encoded(list):
    def tolist(self):
        return list(self)


class EmbeddingModel:
    def encode(self, texts, **_kwargs):
        return Encoded([[float(len(text))] for text in texts])


class Reranker:
    def predict(self, pairs):
        return [len(text) for _, text in pairs]


class Runtime:
    async def predict_async(self, query, trace_recorder, **_kwargs):
        self.cache_scope = _kwargs.get("cache_scope")
        trace_recorder({"component": "local-inference", "status": "succeeded"})
        return f"answer:{query}"

    def model_status(self, identity):
        return {"tenant_id": identity["tenant_id"], "loaded": True}

    def check_and_reload_adapter(self, **_kwargs):
        return True


def test_inference_service_owns_generation_embedding_and_rerank(monkeypatch):
    monkeypatch.setattr(service, "runtime", Runtime())
    monkeypatch.setattr(service, "embedding_model", EmbeddingModel())
    monkeypatch.setattr(service, "reranker", Reranker())
    client = TestClient(service.app)
    headers = {"Authorization": "Bearer development-only"}

    assert (
        client.post(
            "/v1/generate",
            headers=headers,
            json={
                "query": "hello",
                "identity": {"tenant_id": "acme"},
                "cache_scope": "acme:user",
            },
        ).json()["text"]
        == "answer:hello"
    )
    assert service.runtime.cache_scope == "acme:user"
    assert client.post("/v1/embeddings", headers=headers, json={"texts": ["a", "abc"]}).json() == {
        "embeddings": [[1.0], [3.0]]
    }
    assert client.post(
        "/v1/rerank", headers=headers, json={"query": "q", "texts": ["a", "abc"]}
    ).json() == {"scores": [1.0, 3.0]}


def test_inference_service_rejects_missing_credential():
    assert TestClient(service.app).post("/v1/embeddings", json={"texts": ["a"]}).status_code == 401
