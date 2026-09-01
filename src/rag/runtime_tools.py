"""Governed chat tool."""

import asyncio
import time
from typing import Any, Callable

from core.tool_contracts import ToolRegistry, ToolSpec
from rag.answering import answer_with_citations


def register_chat_tool(
    registry: ToolRegistry,
    *,
    chat_adapter_runtime: Any,
    chat_answering: Any,
    chat_retriever: Any,
    chat_context_loader: Callable[[str, str], dict[str, Any]] | None = None,
    chat_result_recorder: Callable[
        [dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]], dict[str, Any]
    ]
    | None = None,
) -> None:
    async def chat(arguments: dict[str, Any]) -> dict[str, Any]:
        identity = arguments.pop("_identity")
        run_context = arguments.pop("_h3_context", {})
        context_ref = arguments.get("context_ref")
        if context_ref is None:
            query = arguments.get("query")
            if not isinstance(query, str) or not query:
                raise ValueError("rag_chat_query_missing")
            context = await asyncio.to_thread(chat_retriever.retrieve, query, identity, top_k=3)
            answer, _, _ = await answer_with_citations(
                query,
                identity,
                context,
                chat_adapter_runtime,
                chat_answering,
            )
            return {"answer": answer}
        if not context_ref.startswith(f"tenants/{identity['tenant_id']}/"):
            raise PermissionError("rag_chat_context_tenant_mismatch")
        if chat_context_loader is None or chat_result_recorder is None:
            raise RuntimeError("rag_chat_capture_not_configured")
        envelope = chat_context_loader(context_ref, arguments["context_sha256"])
        query = envelope.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("rag_chat_query_missing")
        started = time.perf_counter()
        model_calls: list[dict[str, Any]] = []
        try:
            answer, citations, model_execution = await answer_with_citations(
                query,
                identity,
                envelope["retrieval_context"],
                chat_adapter_runtime,
                chat_answering,
                trace_recorder=model_calls.append,
            )
        except Exception as error:
            chat_result_recorder(
                {**run_context, "context_ref": context_ref},
                identity,
                envelope,
                {
                    "answer": "",
                    "citations": [],
                    "model_execution": {},
                    "query": query,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "model_calls": model_calls,
                    "status": "failed",
                    "error_code": type(error).__name__,
                },
            )
            raise
        return chat_result_recorder(
            {**run_context, "context_ref": context_ref},
            identity,
            envelope,
            {
                "answer": answer,
                "citations": citations,
                "model_execution": model_execution,
                "query": query,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "model_calls": model_calls,
                "status": "succeeded",
                "error_code": None,
            },
        )

    registry.register(
        ToolSpec(
            name="rag_chat",
            handler=chat,
            schema={
                "type": "object",
                "required": [],
                "properties": {
                    "query": {"type": "string"},
                    "context_ref": {"type": "string"},
                    "context_sha256": {"type": "string"},
                },
                "additionalProperties": False,
            },
            timeout_seconds=300,
            uses_identity=True,
            idempotent=True,
            max_retries=1,
            scope_resolver=lambda arguments, _identity: (
                [arguments["context_ref"]] if arguments.get("context_ref") else []
            ),
            result_sensitivity={
                "answer": "secret",
                "response_ref": "internal",
                "response_sha256": "internal",
                "snapshot_id": "internal",
                "context_ref": "internal",
                "context_sha256": "internal",
                "document_ids": "internal",
                "citations": "internal",
                "status": "public",
            },
        )
    )
