"""In-memory permission decision cache (30s TTL by default).

Keyed by ``(user, relation, object_)``. Lock-protected for asyncio safety;
single-process — for multi-process deployments back this with Redis instead
(out of scope for Phase 0, see Auth_Design.md §11).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from .types import IntrospectionResponse


@dataclass(slots=True)
class _Entry:
    value: bool
    expires_at: float


class _IntrospectionEntry(NamedTuple):
    response: IntrospectionResponse
    expires_at: float


class PermissionCache:
    """Thread-safe (asyncio) permission decision cache."""

    def __init__(
        self,
        ttl_s: int = 30,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_s = ttl_s
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._store: dict[tuple[str, str, str], _Entry] = {}

    @staticmethod
    def _key(user: str, relation: str, object_: str) -> tuple[str, str, str]:
        return (user, relation, object_)

    async def get(self, user: str, relation: str, object_: str) -> bool | None:
        key = self._key(user, relation, object_)
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._store.pop(key, None)
                return None
            return entry.value

    async def set(self, user: str, relation: str, object_: str, value: bool) -> None:
        key = self._key(user, relation, object_)
        async with self._lock:
            self._store[key] = _Entry(
                value=value,
                expires_at=self._clock() + self.ttl_s,
            )

    async def invalidate(self, user: str, relation: str, object_: str) -> None:
        async with self._lock:
            self._store.pop(self._key(user, relation, object_), None)

    async def invalidate_user(self, user: str) -> None:
        async with self._lock:
            doomed = [k for k in self._store if k[0] == user]
            for k in doomed:
                self._store.pop(k, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


class IntrospectionCache:
    """Cache RFC 7662 introspection responses keyed by sha256(token) hex.

    Both active and inactive responses are cached so that revoked tokens do
    not stampede the auth server. Single-process — back with Redis for
    multi-process deployments.
    """

    def __init__(
        self,
        ttl_s: int = 60,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_s = ttl_s
        self._clock = clock or time.monotonic
        # ``wall_clock`` is epoch seconds (time.time semantics), needed
        # alongside the monotonic cache clock to compare against the token's
        # own ``exp`` claim — RFC 7662 §2.2 epoch seconds. See ``set()``.
        self._wall_clock = wall_clock or time.time
        self._lock = asyncio.Lock()
        self._store: dict[str, _IntrospectionEntry] = {}

    async def get(self, token_sha256: str) -> IntrospectionResponse | None:
        async with self._lock:
            entry = self._store.get(token_sha256)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._store.pop(token_sha256, None)
                return None
            return entry.response

    async def set(self, token_sha256: str, response: IntrospectionResponse) -> None:
        async with self._lock:
            # Cap cache entry lifetime at the token's own ``exp`` (RFC 7662
            # §2.2 epoch seconds) so a cached ``active=True`` response can
            # never outlive the token itself. Round 4 Finding 17 (sage
            # accepted a 50min-expired PAT) was caused by the cache TTL
            # exceeding the token's remaining lifetime: the introspection
            # server correctly returned active=True at first call, the
            # response was cached for the full cache TTL, and subsequent
            # requests after token exp hit a stale active=True cache entry.
            #
            # Tokens without ``exp`` (or inactive responses, which usually
            # omit it) fall back to the configured cache TTL.
            ttl = float(self.ttl_s)
            if response.active and response.exp is not None:
                token_remaining = float(response.exp) - self._wall_clock()
                if token_remaining < ttl:
                    ttl = max(0.0, token_remaining)
            self._store[token_sha256] = _IntrospectionEntry(
                response=response,
                expires_at=self._clock() + ttl,
            )

    async def invalidate(self, token_sha256: str) -> None:
        async with self._lock:
            self._store.pop(token_sha256, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
