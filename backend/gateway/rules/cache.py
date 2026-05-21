"""In-memory rule-list cache scoped by ``(workspace_id, account_id)``.

Phase 0 deferral — Redis-backed cache lands when we go multi-process
(:mod:`backend.shared.authz.cache` has the same pattern + caveat).
``RulesCache`` is a Protocol so swapping in Redis later is mechanical.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class RulesCache(Protocol):
    async def get(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> list[Any] | None: ...

    async def set(
        self,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        rules: list[Any],
    ) -> None: ...

    async def invalidate(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None: ...


@dataclass(slots=True)
class _Entry:
    value: list[Any]
    expires_at: float


class InMemoryRulesCache:
    """Single-process TTL cache, asyncio-safe."""

    def __init__(
        self,
        ttl_s: int = 30,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._store: dict[tuple[uuid.UUID, uuid.UUID], _Entry] = {}

    @staticmethod
    def _key(workspace_id: uuid.UUID, account_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        return (workspace_id, account_id)

    async def get(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> list[Any] | None:
        key = self._key(workspace_id, account_id)
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._store.pop(key, None)
                return None
            return list(entry.value)

    async def set(
        self,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        rules: list[Any],
    ) -> None:
        key = self._key(workspace_id, account_id)
        async with self._lock:
            self._store[key] = _Entry(value=list(rules), expires_at=self._clock() + self._ttl)

    async def invalidate(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        async with self._lock:
            self._store.pop(self._key(workspace_id, account_id), None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
