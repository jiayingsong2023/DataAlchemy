import asyncio
import sys
import os
import json

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from inference.cache import CacheManager

async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/manage_cache.py [clear|stats|list|sessions]")
        return

    command = sys.argv[1]
    cache = CacheManager()
    await cache.connect()

    if command == "clear":
        confirm = input("Are you sure you want to clear ALL cache and sessions? (y/N): ")
        if confirm.lower() == 'y':
            await cache.clear()
            print("All data cleared successfully.")
        else:
            print("Operation cancelled.")
    
    elif command == "stats":
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

    elif command == "sessions":
        print("\n--- User Chat Sessions ---")
        if not cache.redis:
            print("Redis not connected.")
            return
            
        # 查找所有用户的 session 列表键
        keys = await cache.redis.keys("user:*:sessions")
        if not keys:
            print("No user sessions found in Redis.")
            return
            
        for key in keys:
            username = key.split(":")[1]
            print(f"\nUser: {username}")
            sessions = await cache.list_sessions(username)
            for s in sessions:
                print(f"  - ID: {s['id']} | Title: {s['title']} | Created: {s['created_at']}")
                # 获取该 session 的消息数
                msg_count = await cache.redis.llen(f"session:{s['id']}:messages")
                print(f"    (Messages: {msg_count})")

if __name__ == "__main__":
    asyncio.run(main())
