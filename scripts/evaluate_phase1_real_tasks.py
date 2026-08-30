"""Compare direct RAG services with the governed runtime on real tasks."""

# ruff: noqa: E402, I001

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from core.agent_runtime import AgentRuntime, ToolRegistry
from core.runtime_tools import register_runtime_tools
from inference.adapter_runtime import AdapterRuntime
from memory.orchestrator import MemoryOrchestrator
from rag.answering import GroundedAnswering, answer_with_citations
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from config import DATABASE_URL


def answer_hash(answer: object) -> str:
    return hashlib.sha256(str(answer).encode()).hexdigest()


async def evaluate(task_file: Path) -> dict:
    tasks = yaml.safe_load(task_file.read_text(encoding="utf-8"))["tasks"]
    tools = ToolRegistry()
    vector_store = VectorStore()
    retriever = Retriever(vector_store)
    memory = MemoryOrchestrator(DATABASE_URL, vector_store, retriever)
    adapter_runtime = AdapterRuntime()
    answering = GroundedAnswering()
    register_runtime_tools(
        tools,
        vector_store=vector_store,
        memory=memory,
        chat_adapter_runtime=adapter_runtime,
        chat_answering=answering,
        chat_retriever=retriever,
    )
    identity = {"username": "phase1_evaluator", "tenant_id": "default", "role": "admin"}
    runtime_path = Path(os.environ.get("DATA_DIR", "data")) / "phase1_evaluation_runtime.db"
    runtime = AgentRuntime(str(runtime_path), tools)
    results = []
    try:
        for task in tasks:
            start = time.monotonic()
            context = await asyncio.to_thread(retriever.retrieve, task["query"], identity, 3)
            fixed_answer, _, _ = await answer_with_citations(
                task["query"], identity, context, adapter_runtime, answering
            )
            fixed_latency_ms = round((time.monotonic() - start) * 1000, 2)

            start = time.monotonic()
            runtime_task = runtime.create_task(
                identity,
                task["goal"],
                [{"tool": "rag_chat", "arguments": {"query": task["query"]}}],
            )
            completed = await runtime.run(runtime_task["task_id"], identity)
            runtime_latency_ms = round((time.monotonic() - start) * 1000, 2)
            events = runtime.events(runtime_task["task_id"], identity)
            results.append(
                {
                    "id": task["id"],
                    "success": completed["state"] == "succeeded",
                    "fixed_latency_ms": fixed_latency_ms,
                    "runtime_latency_ms": runtime_latency_ms,
                    "steps": completed["current_step"],
                    "tool_success_rate": 1.0 if completed["state"] == "succeeded" else 0.0,
                    "human_takeover": False,
                    "fixed_answer_sha256": answer_hash(fixed_answer),
                    "event_types": [event["event_type"] for event in events],
                }
            )
    finally:
        if adapter_runtime.batch_engine is not None:
            await adapter_runtime.batch_engine.shutdown()
        adapter_runtime.model_manager.clear_cache()
    success_count = sum(result["success"] for result in results)
    return {
        "baseline": "phase1_real_rag_tasks",
        "task_count": len(results),
        "success_count": success_count,
        "completion_rate": success_count / len(results),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=Path("eval/phase1_real_tasks.yaml"))
    parser.add_argument("--output", type=Path, default=Path("eval/phase1_real_task_results.json"))
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.tasks))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    if report["success_count"] != report["task_count"]:
        raise SystemExit(1)
