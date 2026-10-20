"""#929 (B) — the OAuth abuse limits hold across backend replicas.

The limiters were per-process memory: two backend containers would each grant the full
budget, and a deploy reset every bucket. With Redis configured, a limiter keeps its window
in one Redis sorted set shared by every replica (one atomic Lua script per check, so two
replicas cannot both take the last unit). If Redis cannot be reached the limiter degrades to
the old per-process counter rather than letting everything through — and says so in the log.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis as fakeredis_aio
import pytest

from backend.api import oauth
from backend.api.oauth import _SlidingWindowLimiter

pytestmark = pytest.mark.asyncio


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    monkeypatch.setattr(oauth, "_rate_limit_redis", lambda: client)
    return client


def _replica(name: str = "t", *, max_per_window: int = 3) -> _SlidingWindowLimiter:
    """A limiter as one backend process would hold it."""
    return _SlidingWindowLimiter(name=name, window_secs=60, max_per_window=max_per_window)


async def test_two_replicas_share_one_budget(redis: Any) -> None:
    a, b = _replica(), _replica()

    granted = [await (a if i % 2 else b).check_and_record("1.2.3.4") for i in range(5)]

    assert granted == [True, True, True, False, False], "the budget was granted per replica"


async def test_the_budget_is_per_key(redis: Any) -> None:
    a = _replica(max_per_window=1)

    assert await a.check_and_record("1.1.1.1") is True
    assert await a.check_and_record("2.2.2.2") is True
    assert await a.check_and_record("1.1.1.1") is False


async def test_limiters_of_different_routes_do_not_share(redis: Any) -> None:
    token, revoke = _replica("token", max_per_window=1), _replica("revoke", max_per_window=1)

    assert await token.check_and_record("ip") is True
    assert await revoke.check_and_record("ip") is True


async def test_the_failure_counter_shape_holds_across_replicas(redis: Any) -> None:
    """``/token`` asks ``exhausted`` first and ``record``s only rejected credentials."""
    a, b = _replica(max_per_window=2), _replica(max_per_window=2)

    assert await a.exhausted("ip") is False
    await a.record("ip")
    await b.record("ip")
    assert await b.exhausted("ip") is True
    await a.record("ip")  # capped — a flood cannot grow the set
    assert await redis.zcard("bsvibe:ratelimit:t:ip") == 2


async def test_redis_down_degrades_to_the_per_process_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Down:
        async def evalsha(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

        async def eval(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("redis down")

    monkeypatch.setattr(oauth, "_rate_limit_redis", lambda: _Down())
    a = _replica(max_per_window=2)

    assert [await a.check_and_record("ip") for _ in range(3)] == [True, True, False]


async def test_without_redis_the_counter_is_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "_rate_limit_redis", lambda: None)
    a, b = _replica(max_per_window=1), _replica(max_per_window=1)

    assert await a.check_and_record("ip") is True
    assert await b.check_and_record("ip") is True, "two processes, two budgets — the old shape"


async def test_reset_clears_the_shared_buckets_too(redis: Any) -> None:
    a = _replica(max_per_window=1)
    await a.check_and_record("ip")

    await a.areset()

    assert await a.check_and_record("ip") is True
