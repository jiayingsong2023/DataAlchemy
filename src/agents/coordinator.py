import os
import torch
import torch.distributed

# Monkeypatch for ROCm Windows compatibility before any AI imports
if not hasattr(torch.distributed, "is_initialized"):
    torch.distributed.is_initialized = lambda: False
if not hasattr(torch.distributed, "get_rank"):
    torch.distributed.get_rank = lambda: 0

from agents.agent_a import AgentA
from agents.agent_b import AgentB
from agents.agent_c import AgentC
from agents.agent_d import AgentD
from spark_etl.main import get_engine, detect_best_engine
from config import FINAL_OUTPUT_PATH, RAG_CHUNKS_PATH, LLM_CONFIG
import json

class Coordinator:
    """The Orchestrator for all Agents."""
    
    def __init__(self, mode="auto"):
        self.mode = mode
        self.agent_a = AgentA(mode=mode)
        self.agent_b = None # LoRA (Lazy load)
        self.agent_c = AgentC() # Knowledge
        self.agent_d = AgentD() # Finalist

    def run_ingestion_pipeline(self, synthesis=False, max_samples=None):
        """Phase 1: Agent A (Cleaning) -> LLM Synthesis -> Agent C (Indexing)."""
        print("\n" + "=" * 60)
        print("  INGESTION PIPELINE (Agent A -> Synthesis -> Agent C)")
        print("=" * 60)
        
        # 1. Agent A: Data Cleaning
        results = self.agent_a.clean_and_split()
        
        # 2. LLM Synthesis (Optional)
        if synthesis:
            print("\n" + "-" * 40)
            print("  [LLM Synthesis] Generating SFT data via DeepSeek...")
            print("-" * 40)
            try:
                from spark_etl.sft_generator import SFTGenerator
                generator = SFTGenerator()
                # FINAL_OUTPUT_PATH is the cleaned corpus (train.jsonl)
                generator.process_corpus(FINAL_OUTPUT_PATH, max_samples=max_samples)
            except Exception as e:
                print(f"[ERROR] Synthesis failed: {e}")
                print("  Hint: Check your API key in .env")

        # 3. Agent C: Indexing
        if results.get("rag"):
            self.agent_c.build_index(RAG_CHUNKS_PATH)
            
        print("[Coordinator] Ingestion pipeline complete.")

    def run_training_pipeline(self):
        """Phase 2: Agent B (Training)."""
        print("\n" + "=" * 60)
        print("  TRAINING PIPELINE (Agent B)")
        print("=" * 60)
        from train import train
        # Note: In a production scheduler, we might want to pass parameters
        # For now, it calls the standard training logic
        train()
        print("[Coordinator] Training pipeline complete.")

    def run_full_cycle(self):
        """Phase 3: Agent A -> C -> B (The full self-evolution cycle)."""
        print("\n" + "!" * 60)
        print("  STARTING FULL AUTO-EVOLUTION CYCLE")
        print("!" * 60)
        
        # 1. Ingest & Index
        self.run_ingestion_pipeline()
        
        # 2. Fine-tune
        self.run_training_pipeline()
        
        print("\n" + "!" * 60)
        print("  FULL AUTO-EVOLUTION CYCLE COMPLETE")
        print("!" * 60)

    def chat(self, query: str):
        """Phase 2: RAG + LoRA -> Final Answer (Agent C + Agent B -> Agent D)."""
        print(f"\n[Coordinator] Handling query: {query}")
        
        # 1. Agent C: Retrieve Knowledge
        context = self.agent_c.query(query)
        
        # 2. Agent B: Get Model Intuition (Lazy load if needed)
        if self.agent_b is None:
            self.agent_b = AgentB()
        intuition = self.agent_b.predict(query)
        
        # 3. Agent D: Final Fusion
        final_answer = self.agent_d.fuse_and_respond(query, context, intuition)
        return final_answer
