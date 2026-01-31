import os
from typing import Optional
from utils.logger import logger
from config import (
    WASHED_DATA_PATH,
    S3_BUCKET,
    PROCESSED_DATA_DIR,
    RAG_CHUNKS_PATH
)

class PipelineManager:
    """Orchestrates multi-agent pipelines (Ingestion, Training, Full Cycle)."""

    def __init__(self, agent_manager, s3_utils):
        self.agent_manager = agent_manager
        self.s3 = s3_utils

    def run_quant_pipeline(self, input_path: str, output_dir: str):
        """Memory-optimized Numerical Feature Engineering Pipeline."""
        logger.info("Starting Numerical Quant Pipeline")
        self.agent_manager.lazy_load_agents(need_quant=True)

        # 1. Scout: Scan metadata
        schema = self.agent_manager.scout.scan_source(input_path)

        # 2. Validator: Verify integrity
        if not self.agent_manager.validator.validate_schema(schema, schema.columns):
            logger.error("Data validation failed. Aborting.")
            return

        # 3. QuantAgent: Transformations
        temp_poly = os.path.join(output_dir, "poly_features.parquet")
        temp_inter = os.path.join(output_dir, "interaction_features.parquet")
        numeric_cols = [c for c, dt in schema.dtypes.items() if "Int" in dt or "Float" in dt]

        self.agent_manager.quant_agent.generate_poly_features(input_path, temp_poly, numeric_cols[:10])
        self.agent_manager.quant_agent.generate_interaction_terms(temp_poly, temp_inter, numeric_cols[:10])

        # 4. Curator: Feature Selection
        final_output = os.path.join(output_dir, "final_features.parquet")
        self.agent_manager.curator.drop_redundant_features(temp_inter, final_output, numeric_cols)
        logger.info(f"Quant Pipeline complete -> {final_output}")

    def run_ingestion_pipeline(self, stage="all", synthesis=False, max_samples=None):
        """Phase 1: Agent A (Cleaning) -> LLM Synthesis -> Agent C (Indexing)."""
        logger.info(f"Starting Ingestion Pipeline (Stage: {stage})")

        # 1. WASH
        if stage in ["wash", "all"]:
            results = self.agent_manager.agent_a.clean_and_split()
            if stage == "wash":
                return results

        # 2. REFINE
        if stage in ["refine", "all"]:
            if synthesis:
                self._handle_synthesis(max_samples)

            # 3. Indexing
            self.agent_manager.lazy_load_agents(need_c=True)
            actual_chunks_path = RAG_CHUNKS_PATH
            if WASHED_DATA_PATH.startswith("s3"):
                actual_chunks_path = f"{WASHED_DATA_PATH}/rag_chunks.jsonl"
            self.agent_manager.agent_c.build_index(actual_chunks_path)

        logger.info("Ingestion pipeline complete.")

    def _handle_synthesis(self, max_samples):
        """Helper for synthesis and quant during refinement."""
        input_metrics = f"{WASHED_DATA_PATH}/metrics.parquet"
        metrics_exist = self._check_path_exists(input_metrics)

        if metrics_exist:
            quant_output = os.path.join(PROCESSED_DATA_DIR, "quant")
            os.makedirs(quant_output, exist_ok=True)
            self.run_quant_pipeline(input_metrics, quant_output)
        
        try:
            from synthesis.sft_generator import SFTGenerator
            generator = SFTGenerator()
            quant_insight_path = os.path.join(PROCESSED_DATA_DIR, "quant", "final_features.parquet")
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

    def _check_path_exists(self, path: str) -> bool:
        if path.startswith("s3"):
            try:
                prefix = path.split(f"{S3_BUCKET}/")[-1]
                return len(self.s3.list_objects(prefix)) > 0
            except:
                return False
        return os.path.exists(path)

    def run_training_pipeline(self):
        """Phase 2: Agent B (Training)."""
        logger.info("Starting Training Pipeline")
        try:
            from train import train
            train()
        except Exception as e:
            logger.error(f"Training failed: {e}", exc_info=True)
            raise e
        finally:
            import gc
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            gc.collect()

    def run_full_cycle(self, synthesis=False, max_samples=None):
        """Phase 3: The full self-evolution cycle."""
        logger.info("Starting Full Auto-Evolution Cycle")
        self.run_ingestion_pipeline(synthesis=synthesis, max_samples=max_samples)
        self.run_training_pipeline()
        logger.info("Full Auto-Evolution Cycle Complete")
