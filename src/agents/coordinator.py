import asyncio
import datetime
import json
import os
from typing import Any, Callable

from config import FEEDBACK_DATA_DIR
from core.agent_manager import AgentManager
from core.pipeline import PipelineManager
from inference.metrics import LEGACY_AGENT_CALLS
from rag.answering import answer_with_citations
from utils.logger import logger
from utils.s3_utils import S3Utils


class Coordinator:
    """
    The Orchestrator for all Agents.
    Now acts as a Facade delegating to AgentManager and PipelineManager.
    """

    def __init__(self, mode="auto"):
        self.mode = mode
        self.s3 = S3Utils()
        self.agent_manager = AgentManager(mode=mode)
        self.pipeline_manager = PipelineManager(self.agent_manager, self.s3)

        logger.info(f"Coordinator (Facade) initialized in {mode} mode")

    # --- Pipeline Delegation ---

    def run_ingestion_pipeline(self, stage="all", synthesis=False, max_samples=None):
        return self.pipeline_manager.run_ingestion_pipeline(stage, synthesis, max_samples)

    def run_training_pipeline(self):
        return self.pipeline_manager.run_training_pipeline()

    def run_full_cycle(self, synthesis=False, max_samples=None):
        return self.pipeline_manager.run_full_cycle(synthesis, max_samples)

    def run_quant_pipeline(self, input_path: str, output_dir: str):
        return self.pipeline_manager.run_quant_pipeline(input_path, output_dir)

    # --- Agent Management Delegation ---

    def start_knowledge_sync(self):
        self.agent_manager.lazy_load_agents(need_c=True)
        self.agent_manager.agent_c.start_background_sync()

    def reload_model(self, identity=None, expected_release_id=None):
        self.agent_manager.lazy_load_agents(need_b=True)
        return self.agent_manager.agent_b.check_and_reload_adapter(
            force=True, identity=identity, expected_release_id=expected_release_id
        )

    def model_status(self, identity):
        self.agent_manager.lazy_load_agents(need_b=True)
        return self.agent_manager.agent_b.model_status(identity)

    def clear_agents(self):
        self.agent_manager.clear_agents()

    # --- Interaction Logic (Kept in Facade for simplicity) ---

    async def chat_async(
        self,
        query: str,
        identity: dict[str, str],
        cache_scope: str | None = None,
        context: list[dict[str, Any]] | None = None,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
        route: str = "direct",
    ):
        """Async version of chat for WebUI and concurrent processing."""
        LEGACY_AGENT_CALLS.labels(entrypoint="chat_async", route=route).inc()
        logger.info("Handling tenant-scoped query (async)")

        self.agent_manager.lazy_load_agents(need_b=True, need_c=True, need_d=True)

        # 1. Agent C: Retrieve Knowledge
        loop = asyncio.get_event_loop()
        if context is None:
            context = await loop.run_in_executor(
                None, self.agent_manager.agent_c.query, query, identity
            )

        answer, _, _ = await answer_with_citations(
            query,
            identity,
            context,
            self.agent_manager.agent_b,
            self.agent_manager.agent_d,
            cache_scope=cache_scope,
            trace_recorder=trace_recorder,
        )
        return answer

    async def chat_with_citations_async(
        self,
        query: str,
        identity: dict[str, str],
        cache_scope: str | None = None,
        context: list[dict[str, Any]] | None = None,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
        route: str = "direct",
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """Return the normal answer plus citations from the actual retriever rows."""
        LEGACY_AGENT_CALLS.labels(entrypoint="chat_with_citations_async", route=route).inc()
        self.agent_manager.lazy_load_agents(need_b=True, need_c=True, need_d=True)
        loop = asyncio.get_event_loop()
        if context is None:
            context = await loop.run_in_executor(
                None, self.agent_manager.agent_c.query, query, identity
            )
        return await answer_with_citations(
            query,
            identity,
            context,
            self.agent_manager.agent_b,
            self.agent_manager.agent_d,
            cache_scope=cache_scope,
            trace_recorder=trace_recorder,
        )

    def chat(self, query: str, identity: dict[str, str]):
        """Sync wrapper for chat."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio

            nest_asyncio.apply()

        return loop.run_until_complete(self.chat_async(query, identity))

    def save_feedback(
        self,
        query: str,
        answer: str,
        feedback: str = "unrated",
        owner: str | None = None,
        tenant_id: str = "default",
        run_id: str | None = None,
    ):
        """Save user feedback directly to S3/MinIO."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"feedback_{timestamp}.json"

        data = {
            "query": query,
            "answer": answer,
            "feedback": feedback,
            "review_status": "unrated",
            "owner": owner,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "timestamp": datetime.datetime.now().isoformat(),
        }

        try:
            self.s3.put_object(
                s3_key=f"feedback/{filename}",
                body=json.dumps(data, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
            logger.info(f"Feedback saved directly to S3: feedback/{filename}")
            return filename
        except Exception as e:
            logger.error(f"Failed to save feedback directly to S3: {e}")
            # Fallback to local file if S3 fails
            os.makedirs(FEEDBACK_DATA_DIR, exist_ok=True)
            filepath = os.path.join(FEEDBACK_DATA_DIR, f"fallback_{timestamp}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return f"local_{filename}"
