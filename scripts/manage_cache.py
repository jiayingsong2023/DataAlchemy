import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from inference.cache import CacheManager

async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/manage_cache.py [clear|stats]")
        return

    command = sys.argv[1]
    cache = CacheManager()
    await cache.connect()

    if command == "clear":
        confirm = input("Are you sure you want to clear ALL cache? (y/N): ")
        if confirm.lower() == 'y':
            await cache.clear()
            print("Cache cleared successfully.")
        else:
            print("Operation cancelled.")
    
    elif command == "stats":
        # Basic stats if we add more later
        if cache.redis:
            info = await cache.redis.info()
            print(f"Redis Keys: {await cache.redis.dbsize()}")
            print(f"Memory Used: {info.get('used_memory_human')}")
        print(f"Semantic Index Size: {len(cache.semantic_index)}")

    elif command == "list":
        print("\n--- Cached Semantic Entries ---")
        if not cache.semantic_index:
            print("No semantic entries found.")
        for i, item in enumerate(cache.semantic_index):
            print(f"[{i+1}] Prompt: {item['prompt']}")
            print(f"    Result: {item['result'][:100]}...")
            print("-" * 20)

if __name__ == "__main__":
    asyncio.run(main())
