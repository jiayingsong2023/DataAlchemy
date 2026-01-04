import os
import json
import asyncio

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

from agents.agent_a import AgentA
from config import WASHED_DATA_PATH, RAG_CHUNKS_PATH, LLM_CONFIG, FEEDBACK_DATA_DIR
import datetime

class Coordinator:
    """The Orchestrator for all Agents."""
    
    def __init__(self, mode="auto"):
        self.mode = mode
        self.agent_a = AgentA(mode=mode)
        self.agent_b = None # LoRA (Lazy load)
        self.agent_c = None # Knowledge (Lazy load)
        self.agent_d = None # Finalist (Lazy load)

    def _lazy_load_agents(self, need_b=False, need_c=False, need_d=False):
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

    def start_knowledge_sync(self):
        """Start background sync for Agent C."""
        self._lazy_load_agents(need_c=True)
        self.agent_c.start_background_sync()

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
                print("[Coordinator] Washing stage complete.")
                return results

        # 2. Stage: REFINE (LLM Synthesis & Indexing)
        if stage in ["refine", "all"]:
            # A. LLM Synthesis (Optional)
            if synthesis:
                print("\n[Phase 2/3] LLM Synthesis: Generating SFT data...")
                try:
                    from synthesis.sft_generator import SFTGenerator
                    generator = SFTGenerator()
                    # WASHED_DATA_PATH contains the rough-cleaned corpus
                    generator.process_corpus(WASHED_DATA_PATH, max_samples=max_samples)
                except Exception as e:
                    print(f"[ERROR] Synthesis failed: {e}")
                    print("  Hint: Check your API key in .env")

            # B. Agent C: Indexing
            print("\n[Phase 3/3] Agent C: Building RAG Index...")
            if os.path.exists(RAG_CHUNKS_PATH):
                self._lazy_load_agents(need_c=True)
                self.agent_c.build_index(RAG_CHUNKS_PATH)
            else:
                print(f"[WARN] RAG chunks not found at {RAG_CHUNKS_PATH}. Skipping indexing.")
            
        print("[Coordinator] Ingestion pipeline complete.")

    def run_training_pipeline(self):
        """Phase 2: Agent B (Training)."""
        print("\n" + "=" * 60)
        print(f"  TRAINING PIPELINE")
        print("=" * 60)
        
        try:
            from train import train
            train()
            print("[Coordinator] Training pipeline complete.")
        except Exception as e:
            print(f"[ERROR] Training failed: {e}")
            raise e
        finally:
            # Force cleanup after each training run to prevent ROCm leakage
            import torch
            import gc
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
        print(f"\n[Coordinator] Handling query (async): {query}")
        
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

    def save_feedback(self, query: str, answer: str, feedback: str = "good"):
        """Save user feedback to data/feedback directory."""
        os.makedirs(FEEDBACK_DATA_DIR, exist_ok=True)
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"feedback_{timestamp}.json"
        filepath = os.path.join(FEEDBACK_DATA_DIR, filename)
        
        data = {
            "query": query,
            "answer": answer,
            "feedback": feedback,
            "timestamp": datetime.datetime.now().isoformat()
        }
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"[Coordinator] Feedback saved to {filepath} (Status: {feedback})")
        return filename

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
            
        import torch
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        print("[Coordinator] All GPU resources released. Ready for sleep.")
