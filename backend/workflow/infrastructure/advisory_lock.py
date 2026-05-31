"""S3-1 — Postgres advisory locks for cross-instance run dispatch coordination.

When BSNexus runs as multiple uvicorn instances (autoscale, blue/green
overlap), two instances can race to dispatch the same ``ExecutionRun``
— both load the row, both transition pending→running, both fire the
executor. The result is double LLM cost and a corrupted state machine.

The fix is a Postgres ``pg_try_advisory_lock`` keyed by a stable
64-bit hash of the run UUID. The first caller wins and proceeds with
dispatch. The second caller's ``pg_try_advisory_lock`` returns False;
``RunOrchestrator.dispatch_run`` short-circuits as a no-op.

Lifecycle:

  * The lock is **session-scoped** (not transaction-scoped) — the
    orchestrator commits its prep transaction *before* the long
    LLM call, so a transaction lock would release too early. Session
    locks are tied to the SQLAlchemy ``AsyncSession``'s underlying
    Postgres connection.
  * The orchestrator releases the lock explicitly when dispatch
    finishes (success, audit-block, executor-error — all paths). The
    helper is idempotent so duplicate releases are safe.
  * If the dispatcher process dies mid-run (kill -9, OOM), Postgres
    auto-releases the lock at backend disconnect. The run is then
    re-dispatchable by another instance.

SQLite fallback:

  Tests run on SQLite (``aiosqlite``), which has no advisory-lock
  primitive. The helper detects the dialect and falls back to a
  process-local ``asyncio.Lock`` keyed by ``run_id``. This preserves
  the same coordination contract within a single test process — which
  is exactly the scope of the unit tests.

WorkerWatchdog interaction:

  ``WorkerWatchdog`` reclaims orphaned remote-worker runs by polling
  every minute. When it reclaims a run it calls ``dispatch_run`` again
  — the advisory lock makes that safe even if the (now-dead) original
  dispatcher's session is somehow still tracked. In normal operation
  the dead session is gone and the lock is auto-released, so the
  watchdog's reclaim simply succeeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


# Process-local fallback for non-Postgres dialects (test SQLite).
# Keyed by run_id (UUID) — the value is the asyncio.Lock instance.
# We track the holder task so a second acquire from a different task
# observes ``False`` instead of re-entering the same lock.
_FALLBACK_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}
_FALLBACK_HOLDERS: dict[uuid.UUID, asyncio.Task[object] | None] = {}
_FALLBACK_REGISTRY_LOCK = asyncio.Lock()


def advisory_key_for_run(run_id: uuid.UUID) -> int:
    """Derive a 64-bit signed integer key from a run UUID.

    Postgres ``pg_try_advisory_lock(bigint)`` accepts a value in the
    signed-int64 range. We hash the UUID bytes (BLAKE2b-8) for an even
    distribution and reinterpret the unsigned 64-bit result as signed
    so we never overflow the bigint domain.
    """
    digest = hashlib.blake2b(run_id.bytes, digest_size=8).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def try_run_dispatch_lock(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """Attempt to acquire the dispatch advisory lock for ``run_id``.

    Returns ``True`` when the caller now owns the lock, ``False`` when
    another process / task already holds it. Non-blocking — returns
    immediately either way.
    """
    if _is_postgres(session):
        key = advisory_key_for_run(run_id)
        result = await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        acquired = bool(result.scalar())
        if acquired:
            logger.debug("advisory_lock_acquired", run_id=str(run_id), key=key)
        else:
            logger.info("advisory_lock_busy", run_id=str(run_id), key=key)
        return acquired

    # In-process fallback for SQLite tests.
    # Hold the registry lock for the whole check-and-acquire so two
    # concurrent callers can't both see ``locked() == False`` and each
    # acquire the per-run lock in turn.
    async with _FALLBACK_REGISTRY_LOCK:
        lock = _FALLBACK_LOCKS.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return False
        # asyncio.Lock has no non-blocking try_acquire, but holding the
        # registry lock guarantees no other task can race us into the
        # acquire() call here — it returns immediately because we
        # already verified the lock is unlocked above.
        await lock.acquire()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        _FALLBACK_HOLDERS[run_id] = current
        return True


async def release_run_dispatch_lock(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Release the dispatch lock for ``run_id``. Idempotent.

    Postgres ``pg_advisory_unlock`` returns ``False`` if the calling
    session doesn't hold the lock — that's expected on the loser path
    (we still call ``release`` in a ``finally`` block) and is logged
    at debug only.
    """
    if _is_postgres(session):
        key = advisory_key_for_run(run_id)
        try:
            result = await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            released = bool(result.scalar())
            if released:
                logger.debug("advisory_lock_released", run_id=str(run_id), key=key)
            else:
                logger.debug("advisory_lock_not_held", run_id=str(run_id), key=key)
        except Exception:  # noqa: BLE001 — release path must never raise
            logger.warning("advisory_unlock_failed", run_id=str(run_id), exc_info=True)
        return

    # In-process fallback.
    async with _FALLBACK_REGISTRY_LOCK:
        lock = _FALLBACK_LOCKS.get(run_id)
    if lock is None or not lock.locked():
        return
    try:
        lock.release()
    except RuntimeError:
        # Already released — idempotent no-op.
        pass
    _FALLBACK_HOLDERS.pop(run_id, None)
