"""InMemoryRulesCache — TTL + invalidation, single-process."""

from __future__ import annotations

import uuid

from backend.gateway.rules.cache import InMemoryRulesCache


class TestRulesCache:
    async def test_set_then_get(self):
        clock = [0.0]
        cache = InMemoryRulesCache(ttl_s=30, clock=lambda: clock[0])
        ws, acct = uuid.uuid4(), uuid.uuid4()
        await cache.set(ws, acct, ["rule-row"])
        assert await cache.get(ws, acct) == ["rule-row"]

    async def test_miss_returns_none(self):
        cache = InMemoryRulesCache(ttl_s=30)
        assert await cache.get(uuid.uuid4(), uuid.uuid4()) is None

    async def test_ttl_expiry(self):
        clock = [100.0]
        cache = InMemoryRulesCache(ttl_s=30, clock=lambda: clock[0])
        ws, acct = uuid.uuid4(), uuid.uuid4()
        await cache.set(ws, acct, ["x"])
        clock[0] = 200.0  # advance past TTL
        assert await cache.get(ws, acct) is None

    async def test_invalidate(self):
        cache = InMemoryRulesCache(ttl_s=30)
        ws, acct = uuid.uuid4(), uuid.uuid4()
        await cache.set(ws, acct, ["x"])
        await cache.invalidate(ws, acct)
        assert await cache.get(ws, acct) is None

    async def test_account_scoping_separates_entries(self):
        cache = InMemoryRulesCache(ttl_s=30)
        ws = uuid.uuid4()
        acct_a = uuid.uuid4()
        acct_b = uuid.uuid4()
        await cache.set(ws, acct_a, ["a"])
        await cache.set(ws, acct_b, ["b"])
        assert await cache.get(ws, acct_a) == ["a"]
        assert await cache.get(ws, acct_b) == ["b"]
