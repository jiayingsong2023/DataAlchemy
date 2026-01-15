import torch
import torch.distributed
import sys
import os
from config import get_model_config, ADAPTER_S3_PREFIX
from inference.model_manager import ModelManager
from inference.batch_engine import BatchInferenceEngine
from utils.logger import logger
from utils.s3_utils import S3Utils

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "tensor"):
    class Dummy: pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

class AgentB:
    """Agent B: The Model Specialist (LoRA) - Optimized for AMD GPU."""
    
    def __init__(self, model_id: str = None, adapter_path: str = None):
        model_c = get_model_config("model_c")
        # Priority: model_path > model_id
        self.model_id = model_id or model_c.get("model_path") or model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")
        self.adapter_path = adapter_path or model_c.get("adapter_path", "./lora-tiny-llama-adapter")
        
        # Initialize ModelManager and BatchEngine
        self.model_manager = ModelManager()
        self.batch_engine = None
        
    def _ensure_engine(self):
        """Ensure model is loaded and engine is initialized."""
        if self.batch_engine is None:
            # Sync adapter from S3 if available
            s3 = S3Utils()
            if s3.list_objects(ADAPTER_S3_PREFIX):
                logger.info(f"Downloading LoRA adapter from S3: {ADAPTER_S3_PREFIX}...")
                os.makedirs(self.adapter_path, exist_ok=True)
                if s3.download_directory(ADAPTER_S3_PREFIX, self.adapter_path):
                    logger.info("Adapter synced from S3 successfully.")
                else:
                    logger.warning("Failed to sync adapter from S3. Falling back to local if exists.")

            logger.info("Initializing optimized inference engine...")
            self.model_manager.load_models(
                base_model_id=self.model_id,
                lora_adapter_path=self.adapter_path,
                compile_model=True
            )
            self.batch_engine = BatchInferenceEngine(
                model_manager=self.model_manager,
                max_batch_size=4,
                max_wait_ms=50
            )

    async def predict_async(self, user_query: str, max_new_tokens: int = 128) -> str:
        """Get 'intuition' from the fine-tuned model using async batch engine."""
        self._ensure_engine()
        
        prompt = f"### Instruction:\n{user_query}\n\n### Response:\n"
        
        # Use batch engine for inference
        full_response = await self.batch_engine.generate(
            prompt, 
            max_new_tokens=max_new_tokens
        )
        
        if "### Response:" in full_response:
            return full_response.split("### Response:")[-1].strip()
        return full_response[len(prompt):].strip()

    def predict(self, user_query: str, max_new_tokens: int = 128) -> str:
        """Synchronous wrapper for predict_async (for backward compatibility)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if loop.is_running():
            # This is tricky if called from an async context, 
            # but Coordinator is currently sync.
            import nest_asyncio
            nest_asyncio.apply()
            
        return loop.run_until_complete(self.predict_async(user_query, max_new_tokens))

