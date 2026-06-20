import time
from typing import Any


class TTLCache:
    """In-memory key-value cache with per-entry TTL using a monotonic clock."""

    def __init__(self, default_ttl_s: float) -> None:
        self._default_ttl_s = default_ttl_s
        # {key: (value, expiry_monotonic)}
        self._store: dict[Any, tuple[Any, float]] = {}

    def set(self, key: Any, value: Any, ttl_s: float | None = None) -> None:
        ttl = ttl_s if ttl_s is not None else self._default_ttl_s
        self._store[key] = (value, time.monotonic() + ttl)

    def get(self, key: Any, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return default
        value, expiry = entry
        if time.monotonic() < expiry:
            return value
        del self._store[key]
        return default
