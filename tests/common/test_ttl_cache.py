from unittest.mock import patch

import pytest

from backend.common.ttl_cache import TTLCache


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache(default_ttl_s=10.0)


def test_set_and_get_returns_value(cache: TTLCache) -> None:
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_get_missing_key_returns_default(cache: TTLCache) -> None:
    assert cache.get("missing") is None
    assert cache.get("missing", "fallback") == "fallback"


def test_expired_entry_returns_default(cache: TTLCache) -> None:
    start = 100.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache.set("k", "v", ttl_s=5.0)

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 6.0):
        assert cache.get("k", "gone") == "gone"


def test_expired_entry_is_evicted(cache: TTLCache) -> None:
    start = 100.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache.set("k", "v", ttl_s=5.0)

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 6.0):
        cache.get("k")

    # After eviction, internal store no longer holds the key
    assert "k" not in cache._store


def test_not_expired_entry_is_returned(cache: TTLCache) -> None:
    start = 100.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache.set("k", "v", ttl_s=5.0)

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 4.9):
        assert cache.get("k") == "v"


def test_per_call_ttl_overrides_default() -> None:
    cache = TTLCache(default_ttl_s=100.0)
    start = 200.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache.set("k", "v", ttl_s=1.0)

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 2.0):
        assert cache.get("k") is None


def test_overwrite_resets_expiry() -> None:
    start = 100.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache = TTLCache(default_ttl_s=5.0)
        cache.set("k", "first")

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 4.0):
        cache.set("k", "second", ttl_s=10.0)

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 13.0):
        # original TTL from first set would have expired; new expiry is start+4+10=114
        assert cache.get("k") == "second"

    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 15.0):
        assert cache.get("k") is None


def test_boundary_at_exact_expiry_is_treated_as_expired() -> None:
    start = 100.0
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start):
        cache = TTLCache(default_ttl_s=5.0)
        cache.set("k", "v")

    # monotonic == expiry: NOT strictly less than, so expired
    with patch("backend.common.ttl_cache.time.monotonic", return_value=start + 5.0):
        assert cache.get("k") is None
