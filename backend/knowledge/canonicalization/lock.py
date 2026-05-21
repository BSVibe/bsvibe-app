"""MutationLock — single-writer per action_path (Handoff §0.11, Class_Diagram §4).

Slice 1 ships the in-process ``AsyncIOMutationLock`` only. SaaS multi-worker
``RedisMutationLock`` is deferred to v1.x.

Design notes:
- ``asyncio.Lock`` is NOT reentrant. The apply pipeline must never call
  itself recursively on the same ``action_path``; that would deadlock.
- Per-path ``Lock`` objects are stored in a registry guarded by a top-level
  ``registry_lock`` to make registry mutation safe across concurrent acquires.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AcquiredLock:
    """Token returned by ``acquire`` (Class_Diagram §4)."""

    action_path: str
    token: str
    acquired_at: datetime


@dataclass
class _Holder:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_token: str | None = None
    waiters: int = 0


class AsyncIOMutationLock:
    """In-process per-action-path mutation lock (self-host v1).

    Keys are vault-relative action paths. Holders are reference-counted so
    idle paths can be evicted from the registry on release.
    """

    def __init__(self) -> None:
        self._registry: dict[str, _Holder] = {}
        self._registry_lock = asyncio.Lock()

    async def acquire(self, action_path: str) -> AcquiredLock:
        async with self._registry_lock:
            holder = self._registry.get(action_path)
            if holder is None:
                holder = _Holder()
                self._registry[action_path] = holder
            holder.waiters += 1

        try:
            await holder.lock.acquire()
        except BaseException:
            async with self._registry_lock:
                holder.waiters -= 1
                if holder.waiters == 0 and not holder.lock.locked():
                    self._registry.pop(action_path, None)
            raise

        token = secrets.token_hex(8)
        holder.current_token = token
        return AcquiredLock(
            action_path=action_path,
            token=token,
            acquired_at=datetime.now(),
        )

    async def release(self, acquired: AcquiredLock) -> None:
        async with self._registry_lock:
            holder = self._registry.get(acquired.action_path)
            if holder is None:
                msg = f"unknown action path for release: {acquired.action_path!r}"
                raise RuntimeError(msg)
            if holder.current_token != acquired.token:
                msg = (
                    f"lock not held by this token for {acquired.action_path!r}"
                    " (already released or never acquired)"
                )
                raise RuntimeError(msg)
            holder.current_token = None
            holder.waiters -= 1
            holder.lock.release()
            if holder.waiters == 0:
                self._registry.pop(acquired.action_path, None)

    @asynccontextmanager
    async def guard(self, action_path: str) -> AsyncIterator[AcquiredLock]:
        token = await self.acquire(action_path)
        try:
            yield token
        finally:
            await self.release(token)
