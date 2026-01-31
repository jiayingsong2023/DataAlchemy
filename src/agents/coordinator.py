import asyncio
import datetime
import json
import os
from typing import Optional

from core.agent_manager import AgentManager
from core.pipeline import PipelineManager
from utils.logger import logger
from utils.s3_utils import S3Utils
from config import FEEDBACK_DATA_DIR

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

    def reload_model(self):
        self.agent_manager.lazy_load_agents(need_b=True)
        return self.agent_manager.agent_b.check_and_reload_adapter(force=True)

    def clear_agents(self):
        self.agent_manager.clear_agents()

    # --- Interaction Logic (Kept in Facade for simplicity) ---

    async def chat_async(self, query: str):
        """Async version of chat for WebUI and concurrent processing."""
        logger.info(f"Handling query (async): {query}")

        self.agent_manager.lazy_load_agents(need_b=True, need_c=True, need_d=True)

        # 1. Agent C: Retrieve Knowledge
        loop = asyncio.get_event_loop()
        context = await loop.run_in_executor(None, self.agent_manager.agent_c.query, query)

        # 2. Agent B: Get Model Intuition
        intuition = await self.agent_manager.agent_b.predict_async(query)

        # 3. Agent D: Final Fusion
        final_answer = await loop.run_in_executor(
            None,
            self.agent_manager.agent_d.fuse_and_respond,
            query, context, intuition
        )
        return final_answer

    def chat(self, query: str):
        """Sync wrapper for chat."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        return loop.run_until_complete(self.chat_async(query))

    def save_feedback(self, query: str, answer: str, feedback: str = "good"):
        """Save user feedback directly to S3/MinIO."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"feedback_{timestamp}.json"

        data = {
            "query": query,
            "answer": answer,
            "feedback": feedback,
            "timestamp": datetime.datetime.now().isoformat()
        }

        try:
            self.s3.put_object(
                s3_key=f"feedback/{filename}",
                body=json.dumps(data, ensure_ascii=False, indent=2),
                content_type="application/json"
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
