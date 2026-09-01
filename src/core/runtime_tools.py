"""Compose governed tools from their owning domains."""

from typing import Any, Callable

from connectors.runtime_tools import register_connector_tools
from etl.runtime_tools import register_etl_tools
from memory.runtime_tools import register_memory_tools
from rag.runtime_tools import register_chat_tool
from release.runtime_tools import register_release_tools

from .tool_contracts import ToolRegistry


def register_runtime_tools(
    registry: ToolRegistry,
    *,
    vector_store: Any,
    memory: Any,
    chat_adapter_runtime: Any,
    chat_answering: Any,
    chat_retriever: Any,
    chat_context_loader: Callable[[str, str], dict[str, Any]] | None = None,
    chat_result_recorder: Callable[
        [dict[str, Any], dict[str, str], dict[str, Any], dict[str, Any]], dict[str, Any]
    ]
    | None = None,
) -> None:
    register_memory_tools(registry, memory)
    register_chat_tool(
        registry,
        chat_adapter_runtime=chat_adapter_runtime,
        chat_answering=chat_answering,
        chat_retriever=chat_retriever,
        chat_context_loader=chat_context_loader,
        chat_result_recorder=chat_result_recorder,
    )
    register_etl_tools(registry, vector_store=vector_store, chat_retriever=chat_retriever)
    register_release_tools(registry)
    register_connector_tools(registry, vector_store=vector_store)
