"""Permission cache tests — 30s TTL, invalidate, thread-safe."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from backend.shared.authz.types import IntrospectionResponse


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def test_cache_set_and_get() -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)
    await cache.set("user:1", "read", "doc:1", True)
    assert await cache.get("user:1", "read", "doc:1") is True


async def test_cache_miss_returns_none() -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)
    assert await cache.get("user:1", "read", "doc:1") is None


async def test_cache_ttl_expiry() -> None:
    from backend.shared.authz.cache import PermissionCache

    clock = [1000.0]

    def now() -> float:
        return clock[0]

    cache = PermissionCache(ttl_s=30, clock=now)
    await cache.set("u", "r", "o", True)
    clock[0] += 29
    assert await cache.get("u", "r", "o") is True
    clock[0] += 2
    assert await cache.get("u", "r", "o") is None


async def test_cache_invalidate_specific_entry() -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)
    await cache.set("u", "r", "o", True)
    await cache.invalidate("u", "r", "o")
    assert await cache.get("u", "r", "o") is None


async def test_cache_invalidate_user() -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)
    await cache.set("u1", "r", "o1", True)
    await cache.set("u1", "r", "o2", False)
    await cache.set("u2", "r", "o1", True)
    await cache.invalidate_user("u1")
    assert await cache.get("u1", "r", "o1") is None
    assert await cache.get("u1", "r", "o2") is None
    assert await cache.get("u2", "r", "o1") is True


async def test_cache_clear_all() -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)
    await cache.set("u", "r", "o", True)
    await cache.clear()
    assert await cache.get("u", "r", "o") is None


async def test_cache_concurrent_access_is_safe() -> None:
    """The cache must not corrupt under high concurrency. Smoke test."""
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=30)

    async def writer(i: int) -> None:
        await cache.set(f"u{i}", "r", f"o{i}", True)

    async def reader(i: int) -> bool | None:
        return await cache.get(f"u{i}", "r", f"o{i}")

    await asyncio.gather(*(writer(i) for i in range(50)))
    results = await asyncio.gather(*(reader(i) for i in range(50)))
    assert all(r is True for r in results)


@pytest.mark.parametrize("ttl", [1, 30, 300])
async def test_cache_ttl_configurable(ttl: int) -> None:
    from backend.shared.authz.cache import PermissionCache

    cache = PermissionCache(ttl_s=ttl)
    assert cache.ttl_s == ttl


# ------------------------------------------------------------------------
# IntrospectionCache (RFC 7662 response cache, sha256(token)-keyed)
# ------------------------------------------------------------------------


async def test_introspection_cache_set_and_get_hit() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    key = _sha("bsv_sk_abc")
    response = IntrospectionResponse(active=True, sub="user:1", scope=["read"])
    await cache.set(key, response)

    got = await cache.get(key)
    assert got is response


async def test_introspection_cache_miss_returns_none() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    assert await cache.get(_sha("nope")) is None


async def test_introspection_cache_ttl_expiry() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    clock = [1000.0]

    def now() -> float:
        return clock[0]

    cache = IntrospectionCache(ttl_s=60, clock=now)
    key = _sha("tok")
    await cache.set(key, IntrospectionResponse(active=True, sub="u"))
    clock[0] += 59
    hit = await cache.get(key)
    assert hit is not None and hit.active is True
    clock[0] += 2
    assert await cache.get(key) is None


async def test_introspection_cache_invalidate() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    key = _sha("tok")
    await cache.set(key, IntrospectionResponse(active=True, sub="u"))
    await cache.invalidate(key)
    assert await cache.get(key) is None


async def test_introspection_cache_invalidate_unknown_key_is_noop() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    await cache.invalidate(_sha("never-set"))
    assert await cache.get(_sha("never-set")) is None


async def test_introspection_cache_clear_all() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    await cache.set(_sha("a"), IntrospectionResponse(active=True))
    await cache.set(_sha("b"), IntrospectionResponse(active=False))
    await cache.clear()
    assert await cache.get(_sha("a")) is None
    assert await cache.get(_sha("b")) is None


async def test_introspection_cache_caches_inactive_responses() -> None:
    """Inactive (active=false) responses are also cached — avoids re-query storms."""
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)
    key = _sha("revoked")
    inactive = IntrospectionResponse(active=False)
    await cache.set(key, inactive)

    got = await cache.get(key)
    assert got is not None
    assert got.active is False


async def test_introspection_cache_concurrent_access_is_safe() -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=60)

    async def writer(i: int) -> None:
        await cache.set(_sha(f"t{i}"), IntrospectionResponse(active=True, sub=f"u{i}"))

    async def reader(i: int) -> IntrospectionResponse | None:
        return await cache.get(_sha(f"t{i}"))

    await asyncio.gather(*(writer(i) for i in range(50)))
    results = await asyncio.gather(*(reader(i) for i in range(50)))
    assert all(r is not None and r.active for r in results)


@pytest.mark.parametrize("ttl", [1, 60, 300])
async def test_introspection_cache_ttl_configurable(ttl: int) -> None:
    from backend.shared.authz.cache import IntrospectionCache

    cache = IntrospectionCache(ttl_s=ttl)
    assert cache.ttl_s == ttl


# ---------------------------------------------------------------------------
# Round 4 Finding 17 — cache must respect token's own ``exp`` claim
# ---------------------------------------------------------------------------


async def test_introspection_cache_caps_entry_at_token_exp() -> None:
    """The cache's TTL must never extend beyond the token's own ``exp``
    (RFC 7662 §2.2 epoch seconds). Otherwise an ``active=True`` response
    cached near the token's issuance time outlives the token itself —
    a security bug surfaced as Round 4 Finding 17 (sage accepted a
    50-minute-expired PAT)."""
    from backend.shared.authz.cache import IntrospectionCache

    # Monotonic + wall clocks controlled together.
    mono = [0.0]
    wall = [1_000_000.0]
    cache = IntrospectionCache(
        ttl_s=3600,  # cache wants to hold the entry for 1 hour
        clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
    )

    # Token expires in 60s (much shorter than cache TTL).
    token_exp_epoch = int(wall[0] + 60)
    response = IntrospectionResponse(active=True, sub="u", exp=token_exp_epoch)
    await cache.set(_sha("short-lived"), response)

    # Right after set — both clocks still at t=0 — entry valid.
    assert await cache.get(_sha("short-lived")) is not None

    # Advance both clocks past the token's exp (61s).
    mono[0] += 61
    wall[0] += 61
    # Cache TTL says 3600s but token exp was 60s → entry must be expired.
    assert await cache.get(_sha("short-lived")) is None


async def test_introspection_cache_uses_cache_ttl_when_token_exp_missing() -> None:
    """Tokens without an ``exp`` claim (legacy / inactive responses) fall
    back to the configured cache TTL — no regression for that path."""
    from backend.shared.authz.cache import IntrospectionCache

    mono = [0.0]
    wall = [1_000_000.0]
    cache = IntrospectionCache(
        ttl_s=60,
        clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
    )
    await cache.set(_sha("no-exp"), IntrospectionResponse(active=True, sub="u"))

    mono[0] += 30
    wall[0] += 30
    assert await cache.get(_sha("no-exp")) is not None

    mono[0] += 31
    wall[0] += 31
    assert await cache.get(_sha("no-exp")) is None


async def test_introspection_cache_inactive_response_ignores_token_exp() -> None:
    """Inactive responses are cached for the full cache TTL even if they
    carry an ``exp`` claim — they exist to suppress re-query storms on
    revoked tokens, not to track liveness."""
    from backend.shared.authz.cache import IntrospectionCache

    mono = [0.0]
    wall = [1_000_000.0]
    cache = IntrospectionCache(
        ttl_s=60,
        clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
    )
    inactive = IntrospectionResponse(active=False, exp=int(wall[0] + 10))
    await cache.set(_sha("revoked"), inactive)

    # Past the token exp but inside cache TTL → still cached as inactive.
    mono[0] += 30
    wall[0] += 30
    got = await cache.get(_sha("revoked"))
    assert got is not None
    assert got.active is False
