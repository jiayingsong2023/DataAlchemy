import asyncio

import pytest

from src.inference.batch_engine import BatchInferenceEngine
from src.inference.cache import CacheManager


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start : None if end == -1 else end + 1]

    async def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start : None if end == -1 else end + 1]

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.lists.pop(key, None)

    async def scan_iter(self, pattern):
        prefix = pattern.removesuffix("*")
        for key in [*self.values, *self.lists]:
            if key.startswith(prefix):
                yield key


@pytest.mark.asyncio
async def test_session_access_requires_owner():
    cache = CacheManager(enable_semantic=False)
    cache.redis = FakeRedis()
    session_id = await cache.create_session("alice")

    await cache.add_message_to_session("alice", session_id, {"query": "secret"})
    assert await cache.get_session_messages("alice", session_id) == [{"query": "secret"}]

    with pytest.raises(PermissionError):
        await cache.get_session_messages("bob", session_id)
    with pytest.raises(PermissionError):
        await cache.add_message_to_session("bob", session_id, {"query": "steal"})


@pytest.mark.asyncio
async def test_clear_only_deletes_dataalchemy_keys():
    cache = CacheManager(enable_semantic=False)
    cache.redis = FakeRedis()
    await cache.redis.set("dataalchemy:cache:exact:one", "cached")
    await cache.redis.set("other-service:key", "keep")

    await cache.clear()

    assert await cache.redis.get("dataalchemy:cache:exact:one") is None
    assert await cache.redis.get("other-service:key") == "keep"


def test_cache_key_is_scoped_by_user():
    cache = CacheManager(enable_semantic=False)
    assert cache._get_exact_key("prompt", {}, "alice") != cache._get_exact_key("prompt", {}, "bob")


class RecordingModelManager:
    def __init__(self):
        self.calls = []

    def generate(self, prompts, generation_kwargs):
        self.calls.append((prompts, generation_kwargs))
        return [f"{prompt}:{generation_kwargs['max_new_tokens']}" for prompt in prompts]


@pytest.mark.asyncio
async def test_batch_engine_does_not_mix_generation_parameters():
    manager = RecordingModelManager()
    engine = BatchInferenceEngine(manager, max_batch_size=2, max_wait_ms=1, enable_cache=False)

    first, second = await asyncio.gather(
        engine.generate("first", max_new_tokens=8),
        engine.generate("second", max_new_tokens=16),
    )

    assert first == "first:8"
    assert second == "second:16"
    assert {call[1]["max_new_tokens"] for call in manager.calls} == {8, 16}
