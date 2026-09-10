"""GPU-only model service; Web never imports model runtimes."""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder, SentenceTransformer

from config import get_model_config
from inference.adapter_runtime import AdapterRuntime

app = FastAPI(title="DataAlchemy inference")
runtime = AdapterRuntime()
embedding_model: Any = None
reranker: Any = None


def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {os.getenv('INFERENCE_API_TOKEN', 'development-only')}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid inference credential")


class Generate(BaseModel):
    query: str
    identity: dict[str, str] | None = None
    cache_scope: str | None = None
    max_new_tokens: int = 128


class Texts(BaseModel):
    texts: list[str]


class Rerank(Texts):
    query: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/generate", dependencies=[Depends(authorize)])
async def generate(request: Generate) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    text = await runtime.predict_async(
        request.query,
        identity=request.identity,
        cache_scope=request.cache_scope,
        max_new_tokens=request.max_new_tokens,
        trace_recorder=calls.append,
    )
    return {
        "text": text,
        "model_execution": runtime.model_status(request.identity),
        "model_call": calls[-1],
    }


@app.post("/v1/embeddings", dependencies=[Depends(authorize)])
def embeddings(request: Texts) -> dict[str, list[list[float]]]:
    global embedding_model
    if embedding_model is None:
        model = get_model_config("model_b")
        embedding_model = SentenceTransformer(
            model.get("model_path") or model.get("model_id", "BAAI/bge-small-zh-v1.5"),
            device="cuda",
        )
    return {"embeddings": embedding_model.encode(request.texts, convert_to_numpy=True).tolist()}


@app.post("/v1/rerank", dependencies=[Depends(authorize)])
def rerank(request: Rerank) -> dict[str, list[float]]:
    global reranker
    if reranker is None:
        model = get_model_config("model_b")
        reranker = CrossEncoder(
            model.get("reranker_path") or model.get("reranker_id", "BAAI/bge-reranker-base"),
            device="cuda",
        )
    return {"scores": [float(score) for score in reranker.predict([[request.query, text] for text in request.texts])]}


@app.post("/v1/model-status", dependencies=[Depends(authorize)])
def model_status(body: dict[str, Any]) -> dict[str, Any]:
    return runtime.model_status(body.get("identity"))


@app.post("/v1/reload", dependencies=[Depends(authorize)])
def reload(body: dict[str, Any]) -> dict[str, bool]:
    return {
        "reloaded": runtime.check_and_reload_adapter(
            force=bool(body.get("force")),
            identity=body.get("identity"),
            expected_release_id=body.get("expected_release_id"),
        )
    }
