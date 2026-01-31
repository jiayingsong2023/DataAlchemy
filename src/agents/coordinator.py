import asyncio
import json
import os


# Lazy import for torch to allow running Agent A without AI libs
def apply_torch_patches():
    try:
        import torch
        import torch.distributed
        # Monkeypatch for ROCm Windows compatibility before any AI imports
        if not hasattr(torch.distributed, "is_initialized"):
            torch.distributed.is_initialized = lambda: False
        if not hasattr(torch.distributed, "get_rank"):
            torch.distributed.get_rank = lambda: 0
    except ImportError:
        pass

apply_torch_patches()

import datetime

from agents.agent_a import AgentA
from config import (
    FEEDBACK_DATA_DIR,
    PROCESSED_DATA_DIR,
    RAG_CHUNKS_PATH,
    S3_BUCKET,
    WASHED_DATA_PATH,
)
from utils.logger import logger
from utils.s3_utils import S3Utils


class Coordinator:
    """The Orchestrator for all Agents."""

    def __init__(self, mode="auto"):
        self.mode = mode
        self.agent_a = AgentA(mode=mode)
        self.agent_b = None # LoRA (Lazy load)
        self.agent_c = None # Knowledge (Lazy load)
        self.agent_d = None # Finalist (Lazy load)
        self.s3 = S3Utils()

        # Numerical Quant Agents (Lazy load)
        # ... (same as before)
        self.scout = None
        self.quant_agent = None
        self.validator = None
        self.curator = None

        logger.info(f"Coordinator initialized in {mode} mode")

    def _lazy_load_agents(self, need_b=False, need_c=False, need_d=False, need_quant=False):
        """Helper to load AI agents only when needed."""
        if need_c and self.agent_c is None:
            from agents.agent_c import AgentC
            self.agent_c = AgentC()
        if need_b and self.agent_b is None:
            from agents.agent_b import AgentB
            self.agent_b = AgentB()
        if need_d and self.agent_d is None:
            from agents.agent_d import AgentD
            self.agent_d = AgentD()

        if need_quant:
            if self.scout is None:
                from agents.quant.scout import ScoutAgent
                self.scout = ScoutAgent()
            if self.quant_agent is None:
                from agents.quant.quant_agent import QuantAgent
                self.quant_agent = QuantAgent()
            if self.validator is None:
                from agents.quant.validator import ValidatorAgent
                self.validator = ValidatorAgent()
            if self.curator is None:
                from agents.quant.curator import CuratorAgent
                self.curator = CuratorAgent()

    def start_knowledge_sync(self):
        """Start background sync for Agent C."""
        self._lazy_load_agents(need_c=True)
        self.agent_c.start_background_sync()

    def run_quant_pipeline(self, input_path: str, output_dir: str):
        """
        Memory-optimized Numerical Feature Engineering Pipeline.
        Scout -> Validator -> QuantAgent -> Curator.
        """
        print("\n" + "=" * 60)
        print("  NUMERICAL QUANT PIPELINE")
        print("=" * 60)

        self._lazy_load_agents(need_quant=True)

        # 1. Scout: Scan metadata
        print("\n[Phase 1/4] Scout: Inferring Schema...")
        schema = self.scout.scan_source(input_path)

        # 2. Validator: Verify integrity
        print("\n[Phase 2/4] Validator: Checking Data Integrity...")
        if not self.validator.validate_schema(schema, schema.columns):
            logger.error("Data validation failed. Aborting.")
            return

        # 3. QuantAgent: High-dimensional transformations (Polars Streaming)
        print("\n[Phase 3/4] QuantAgent: Generating Polynomial & Interaction Features...")
        temp_poly = os.path.join(output_dir, "poly_features.parquet")
        temp_inter = os.path.join(output_dir, "interaction_features.parquet")

        # Select numeric columns for poly/interaction
        numeric_cols = [c for c, dt in schema.dtypes.items() if "Int" in dt or "Float" in dt]

        self.quant_agent.generate_poly_features(input_path, temp_poly, numeric_cols[:10]) # Limit to 10 for safety
        self.quant_agent.generate_interaction_terms(temp_poly, temp_inter, numeric_cols[:10])

        # 4. Curator: Feature Selection (Chunked Correlation)
        print("\n[Phase 4/4] Curator: Filtering Redundant Features...")
        final_output = os.path.join(output_dir, "final_features.parquet")
        self.curator.drop_redundant_features(temp_inter, final_output, numeric_cols)

        print("\n" + "=" * 60)
        print(f"  QUANT PIPELINE COMPLETE -> {final_output}")
        print("=" * 60)

    def run_ingestion_pipeline(self, stage="all", synthesis=False, max_samples=None):
        """
        Phase 1: Agent A (Cleaning) -> LLM Synthesis (Refining) -> Agent C (Indexing).
        
        Args:
            stage: 'wash' (Agent A only), 'refine' (LLM + Indexing), or 'all'.
            synthesis: Whether to run LLM synthesis during 'refine' or 'all'.
            max_samples: Limit for LLM synthesis.
        """
        print("\n" + "=" * 60)
        print(f"  INGESTION PIPELINE (Stage: {stage.upper()})")
        print("=" * 60)

        # 1. Stage: WASH (Agent A: Data Cleaning)
        if stage in ["wash", "all"]:
            print("\n[Phase 1/3] Agent A: Rough Cleaning...")
            results = self.agent_a.clean_and_split()
            # If we only wanted to wash, we stop here
            if stage == "wash":
                logger.info("Washing stage complete.")
                return results

        # 2. Stage: REFINE (Numerical Quant + LLM Synthesis & Indexing)
        if stage in ["refine", "all"]:
            # A. Numerical Quant (Optional - only if synthesis is on)
            if synthesis:
                print("\n[Phase 2a/3] Quant: Refining numerical insights for synthesis...")
                input_metrics = f"{WASHED_DATA_PATH}/metrics.parquet"

                # Check if metrics exist before running quant
                metrics_exist = False
                if input_metrics.startswith("s3"):
                    try:
                        # Extract prefix
                        prefix = input_metrics.split(f"{S3_BUCKET}/")[-1]
                        objects = self.s3.list_objects(prefix)
                        metrics_exist = len(objects) > 0
                    except Exception as e:
                        logger.warning(f"Failed to check metrics existence on S3: {e}")
                else:
                    metrics_exist = os.path.exists(input_metrics)

                if metrics_exist:
                    quant_output = os.path.join(PROCESSED_DATA_DIR, "quant")
                    self.run_quant_pipeline(input_metrics, quant_output)
                else:
                    logger.warning(f"Metrics parquet not found at {input_metrics}. Skipping Numerical Quant.")

            # B. LLM Synthesis (Optional)
            if synthesis:
                print("\n[Phase 2b/3] LLM Synthesis: Generating SFT data...")
                try:
                    from synthesis.sft_generator import SFTGenerator
                    generator = SFTGenerator()

                    # Pass the quant insights if they exist
                    quant_insight_path = os.path.join(PROCESSED_DATA_DIR, "quant", "final_features.parquet")

                    # WASHED_DATA_PATH contains the output root (e.g. s3a://bucket/processed)
                    corpus_path = WASHED_DATA_PATH
                    if corpus_path.startswith("s3"):
                        corpus_path = f"{corpus_path}/cleaned_corpus.jsonl"

                    generator.process_corpus(
                        corpus_path,
                        max_samples=max_samples,
                        insight_path=quant_insight_path if os.path.exists(quant_insight_path) else None
                    )
                except Exception as e:
                    logger.error(f"Synthesis failed: {e}", exc_info=True)
                    logger.info("  Hint: Check your API key in .env")

            # C. Agent C: Indexing
            print("\n[Phase 3/3] Agent C: Building RAG Index...")
            # Decide path based on WASHED_DATA_PATH (S3 or Local)
            actual_chunks_path = RAG_CHUNKS_PATH
            if WASHED_DATA_PATH.startswith("s3"):
                # S3 output from Spark is already in the processed folder
                actual_chunks_path = f"{WASHED_DATA_PATH}/rag_chunks.jsonl"

            self._lazy_load_agents(need_c=True)
            self.agent_c.build_index(actual_chunks_path)

        logger.info("Ingestion pipeline complete.")

    def run_training_pipeline(self):
        """Phase 2: Agent B (Training)."""
        print("\n" + "=" * 60)
        print("  TRAINING PIPELINE")
        print("=" * 60)

        try:
            from train import train
            train()
            print("[Coordinator] Training pipeline complete.")
        except Exception as e:
            logger.error(f"Training failed: {e}", exc_info=True)
            raise e
        finally:
            # Force cleanup after each training run to prevent ROCm leakage
            import gc

            import torch
            torch.cuda.empty_cache()
            gc.collect()

    def run_full_cycle(self, synthesis=False, max_samples=None):
        """Phase 3: Agent A -> C -> B (The full self-evolution cycle)."""
        print("\n" + "!" * 60)
        print("  STARTING FULL AUTO-EVOLUTION CYCLE")
        print("!" * 60)

        # 1. Ingest & Index (will lazy load C inside)
        self.run_ingestion_pipeline(synthesis=synthesis, max_samples=max_samples)

        # 2. Fine-tune
        self.run_training_pipeline()

        print("\n" + "!" * 60)
        print("  FULL AUTO-EVOLUTION CYCLE COMPLETE")
        print("!" * 60)

    def chat(self, query: str):
        """Phase 2: RAG + LoRA -> Final Answer (Agent C + Agent B -> Agent D)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()

        return loop.run_until_complete(self.chat_async(query))

    async def chat_async(self, query: str):
        """Async version of chat for WebUI and concurrent processing."""
        logger.info(f"Handling query (async): {query}")

        self._lazy_load_agents(need_b=True, need_c=True, need_d=True)

        # 1. Agent C: Retrieve Knowledge (Run in executor as it's currently sync)
        loop = asyncio.get_event_loop()
        context = await loop.run_in_executor(None, self.agent_c.query, query)

        # 2. Agent B: Get Model Intuition (Already async-ready)
        intuition = await self.agent_b.predict_async(query)

        # 3. Agent D: Final Fusion (Run in executor as it's currently sync)
        final_answer = await loop.run_in_executor(
            None,
            self.agent_d.fuse_and_respond,
            query, context, intuition
        )
        return final_answer

    def reload_model(self):
        """Force check and reload the latest model from S3."""
        self._lazy_load_agents(need_b=True)
        return self.agent_b.check_and_reload_adapter(force=True)

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

    def clear_agents(self):
        """Deep clean: Remove all agent instances and release GPU memory."""
        print("\n[Coordinator] Deep cleaning AI agents and releasing resources...")

        if self.agent_b:
            del self.agent_b
            self.agent_b = None
        if self.agent_c:
            self.agent_c.stop_background_sync()
            del self.agent_c
            self.agent_c = None
        if self.agent_d:
            del self.agent_d
            self.agent_d = None

        import gc

        import torch
        gc.collect()
        torch.cuda.empty_cache()
        logger.info("All GPU resources released. Ready for sleep.")
