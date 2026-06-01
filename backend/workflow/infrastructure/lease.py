# bsvibe:stable-internal — modifications require a design doc update.
# Owners: workflow/infrastructure
"""Lift J — per-workspace advisory leases for worker-scope mutexes.

Companion to :mod:`backend.workflow.infrastructure.advisory_lock` (which
locks at the *per-run* granularity for orchestrator dispatch). Some
workers do work whose unit of contention is the **workspace**, not a
single row:

* :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`
  runs the canonical-pattern promoter per affected workspace after a
  batch. Two servers draining settle activities for the same workspace
  must NOT run the promoter concurrently — the promoter is idempotent
  by design (Knowledge §5 ratchet), but two concurrent runs duplicate
  the LLM cost + write contention. The workspace lease serialises them
  cheaply.

Mechanism follows v8 §11.2 exactly: ``pg_try_advisory_lock(bigint)``
keyed by a stable 64-bit hash of the workspace UUID, with a process-
local ``asyncio.Lock`` fallback for SQLite tests. Lifecycle is session-
scoped — release in a ``finally`` block. The key derivation uses a
DIFFERENT digest namespace than ``advisory_lock.advisory_key_for_run``
so a workspace lease and a run-id lease keyed off the same UUID bytes
do not collide.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class _WorkspaceFallbackRegistry:
    """Process-local workspace-lease fallback for non-Postgres dialects.

    Lift N defensive pattern #3 (v8 §22 / D45). The registry lock guards
    the check-and-acquire so two concurrent callers can't both observe
    ``locked()==False`` and each acquire in turn.
    """

    __slots__ = ("_locks", "_registry_lock")

    def __init__(self) -> None:
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    @property
    def registry_lock(self) -> asyncio.Lock:
        return self._registry_lock

    def get_or_create_lock(self, workspace_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(workspace_id, asyncio.Lock())

    def get_lock(self, workspace_id: uuid.UUID) -> asyncio.Lock | None:
        return self._locks.get(workspace_id)


_FALLBACK: Final[_WorkspaceFallbackRegistry] = _WorkspaceFallbackRegistry()

# Domain salt — distinguishes the workspace-lease key space from the
# run-dispatch key space in :mod:`advisory_lock`. Two leases keyed off
# the same UUID bytes (a workspace whose id collides numerically with a
# run id) MUST land on different bigints, otherwise a run-dispatch lock
# could falsely refuse a workspace-promote acquire (or vice versa).
_WORKSPACE_PROMOTE_SALT = b"bsvibe.workspace.promote/"


def workspace_promote_key(workspace_id: uuid.UUID) -> int:
    """Stable signed-int64 hash of ``workspace_id`` for ``pg_try_advisory_lock``."""
    digest = hashlib.blake2b(_WORKSPACE_PROMOTE_SALT + workspace_id.bytes, digest_size=8).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def try_workspace_promote_lock(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """Attempt to acquire the per-workspace promote lease. Non-blocking.

    Returns ``True`` on acquire, ``False`` when another caller / server
    holds it. The PG path uses ``pg_try_advisory_lock``; the SQLite
    fallback uses a process-local ``asyncio.Lock`` so test races are
    meaningful in-process.
    """
    if _is_postgres(session):
        key = workspace_promote_key(workspace_id)
        result = await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        acquired = bool(result.scalar())
        if acquired:
            logger.debug("workspace_promote_lock_acquired", workspace_id=str(workspace_id), key=key)
        else:
            logger.info("workspace_promote_lock_busy", workspace_id=str(workspace_id), key=key)
        return acquired

    # In-process fallback for SQLite tests.
    async with _FALLBACK.registry_lock:
        lock = _FALLBACK.get_or_create_lock(workspace_id)
        if lock.locked():
            return False
        # ``asyncio.Lock`` has no non-blocking try_acquire, but holding the
        # registry lock guarantees no other task can race us into this
        # ``acquire()`` — it returns immediately since we already verified
        # ``locked()`` is False above.
        await lock.acquire()
        return True


async def release_workspace_promote_lock(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Release the per-workspace promote lease. Idempotent (release path)."""
    if _is_postgres(session):
        key = workspace_promote_key(workspace_id)
        try:
            result = await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            released = bool(result.scalar())
            if released:
                logger.debug(
                    "workspace_promote_lock_released",
                    workspace_id=str(workspace_id),
                    key=key,
                )
            else:
                logger.debug(
                    "workspace_promote_lock_not_held",
                    workspace_id=str(workspace_id),
                    key=key,
                )
        except Exception:  # noqa: BLE001 — release path must never raise
            logger.warning(
                "workspace_promote_unlock_failed",
                workspace_id=str(workspace_id),
                exc_info=True,
            )
        return

    async with _FALLBACK.registry_lock:
        lock = _FALLBACK.get_lock(workspace_id)
    if lock is None or not lock.locked():
        return
    try:
        lock.release()
    except RuntimeError:
        # Already released — idempotent no-op.
        pass


__all__ = [
    "release_workspace_promote_lock",
    "try_workspace_promote_lock",
    "workspace_promote_key",
]
