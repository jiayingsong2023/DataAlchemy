"""Web control-plane client for the isolated inference service."""

from __future__ import annotations

import os
from typing import Any

import httpx


class InferenceClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("INFERENCE_URL", "http://inference:8001")).rstrip("/")
        token = os.getenv("INFERENCE_API_TOKEN", "development-only")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = httpx.request(
            method, f"{self.base_url}{path}", json=payload, headers=self.headers, timeout=30.0
        )
        response.raise_for_status()
        return response.json()

    async def predict_async(self, query: str, **kwargs: Any) -> str:
        payload = {
            "query": query,
            "identity": kwargs.get("identity"),
            "cache_scope": kwargs.get("cache_scope"),
            "max_new_tokens": kwargs.get("max_new_tokens", 128),
        }
        async with httpx.AsyncClient(headers=self.headers, timeout=120.0) as client:
            response = await client.post(f"{self.base_url}/v1/generate", json=payload)
        response.raise_for_status()
        result = response.json()
        if recorder := kwargs.get("trace_recorder"):
            recorder(result["model_call"])
        return result["text"]

    def model_status(self, identity: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("POST", "/v1/model-status", {"identity": identity})

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._request("POST", "/v1/embeddings", {"texts": texts})["embeddings"]

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        return self._request("POST", "/v1/rerank", {"query": query, "texts": texts})["scores"]

    def check_and_reload_adapter(
        self,
        force: bool = False,
        identity: dict[str, str] | None = None,
        expected_release_id: str | None = None,
    ) -> bool:
        result = self._request(
            "POST",
            "/v1/reload",
            {
                "force": force,
                "identity": identity,
                "expected_release_id": expected_release_id,
            },
        )
        return bool(result["reloaded"])
