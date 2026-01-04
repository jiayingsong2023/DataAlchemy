import asyncio
from src.inference.cache import CacheManager

async def test_cache():
    cache = CacheManager()
    await cache.connect()
    await cache.clear()
    
    prompt = "What is the capital of France?"
    result = "The capital of France is Paris."
    kwargs = {"max_new_tokens": 50}
    
    print("\n[1] Setting cache...")
    await cache.set(prompt, kwargs, result)
    
    print("\n[2] Testing exact match...")
    hit = await cache.get(prompt, kwargs)
    print(f"Exact hit: {hit}")
    assert hit == result
    
    print("\n[3] Testing semantic match...")
    similar_prompt = "Tell me the capital of France"
    semantic_hit = await cache.get(similar_prompt, kwargs)
    print(f"Semantic hit: {semantic_hit}")
    assert semantic_hit == result
    
    print("\n[4] Testing persistence (re-connecting)...")
    new_cache = CacheManager()
    await new_cache.connect()
    
    persistence_hit = await new_cache.get(similar_prompt, kwargs)
    print(f"Persistence semantic hit: {persistence_hit}")
    assert persistence_hit == result
    
    print("\nAll cache tests passed!")

if __name__ == "__main__":
    asyncio.run(test_cache())
