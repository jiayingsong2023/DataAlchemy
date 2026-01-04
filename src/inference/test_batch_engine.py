"""
Test script for batch inference engine
Run this to verify the core engine works on AMD AI Max+ 395
"""
import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from inference.model_manager import ModelManager
from inference.batch_engine import BatchInferenceEngine


async def test_batch_engine():
    """Test batch inference engine"""
    print("="*60)
    print("Testing Batch Inference Engine on AMD AI Max+ 395")
    print("="*60)
    
    # Initialize model manager
    print("\n[1/4] Initializing Model Manager...")
    model_manager = ModelManager()
    
    # Load models (adjust paths as needed)
    base_model_path = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
    lora_adapter_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "lora-tiny-llama-adapter")
    
    if not os.path.exists(lora_adapter_path):
        print(f"[WARN] LoRA adapter not found at {lora_adapter_path}, using base model only")
        lora_adapter_path = None
    
    model_manager.load_models(
        base_model_id=base_model_path,
        lora_adapter_path=lora_adapter_path,
        compile_model=True  # Enable torch.compile
    )
    
    print(f"\n[2/4] Model loaded successfully!")
    print(f"Memory usage: {model_manager.get_memory_usage()}")
    
    # Initialize batch engine
    print("\n[3/4] Initializing Batch Engine...")
    batch_engine = BatchInferenceEngine(
        model_manager=model_manager,
        max_batch_size=4,
        max_wait_ms=50,
        cache_size=100,
        enable_cache=True
    )
    
    # Test queries
    test_queries = [
        "What is machine learning?",
        "Explain neural networks",
        "What is deep learning?",
        "What is machine learning?",  # Duplicate to test cache
    ]
    
    print(f"\n[4/4] Running {len(test_queries)} test queries...")
    print("-"*60)
    
    # Run queries concurrently
    tasks = [batch_engine.generate(query) for query in test_queries]
    results = await asyncio.gather(*tasks)
    
    # Display results
    for i, (query, result) in enumerate(zip(test_queries, results), 1):
        print(f"\nQuery {i}: {query}")
        print(f"Response: {result[:200]}...")  # Show first 200 chars
    
    # Show statistics
    print("\n" + "="*60)
    print("Batch Engine Statistics:")
    print("="*60)
    stats = batch_engine.get_stats()
    for key, value in stats.items():
        if isinstance(value, dict):
            print(f"\n{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        else:
            print(f"{key}: {value}")
    
    print("\n" + "="*60)
    print("Test completed successfully!")
    print("="*60)
    
    # Clean up resources
    print("\n[Cleanup] Releasing resources...")
    await batch_engine.shutdown()
    model_manager.clear_cache()
    
    print("[Cleanup] Complete!")


if __name__ == "__main__":
    try:
        asyncio.run(test_batch_engine())
    finally:
        # Force exit to prevent hanging (similar to webui/app.py)
        import os
        import sys
        sys.stdout.flush()
        os._exit(0)
