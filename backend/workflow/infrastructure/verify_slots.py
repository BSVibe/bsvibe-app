"""Concurrency slots for the disposable full-surface verification stack.

Full-surface verification stands a product's whole stack up, per run, on the
SAME machine that runs production, then tears it down. Two constraints meet
here:

* **Disk.** A full disk on this Mac Mini is an unrecoverable brick, so the
  number of simultaneous stacks is bounded. The bound is a per-workspace DB
  setting rather than a constant because it is a **plan tier** ("N concurrent
  verifications"), not an implementation detail.
* **Orphans.** A run whose process dies never reaches its ``finally``, so its
  stack survives. A naive counter would leak that slot forever and the limit
  would become a deadlock — the feature switching itself off.

The resolution is one mechanism, not two:

1. A slot is held by a **session-scoped PG advisory lock**. When the holder's
   connection dies, the DATABASE releases it. No TTL, no heartbeat, no reaper.
2. The compose project is named after the **slot**, not the run. So the next
   acquirer of slot *i* collides with exactly one predecessor's leftovers, and
   tears them down before starting.

∴ **reclaiming the slot IS reclaiming the stack.** (See
``~/Docs/BSVibe_Production_Verification_Design.md`` §3.3.3.)

The SQLite fallback mirrors :mod:`backend.workflow.infrastructure.lease`: an
in-process registry so test races are meaningful, while the real property —
the database freeing a dead holder's lock — is exercised only against PG.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

#: Domain salt — keeps this key space disjoint from the workspace-promote and
#: run-dispatch lease key spaces. Two subsystems hashing onto the same bigint
#: would let one silently refuse the other's acquire.
_VERIFY_SLOT_SALT: Final[bytes] = b"bsvibe.verify.slot/"

#: Slot count when the workspace has not set one. Deliberately 1: the safe
#: default on a single founder machine is "one stack at a time".
DEFAULT_VERIFY_SLOTS: Final[int] = 1


def verify_slot_key(index: int) -> int:
    """Stable signed-int64 advisory-lock key for verification slot ``index``."""
    digest = hashlib.blake2b(
        _VERIFY_SLOT_SALT + index.to_bytes(4, "big", signed=False), digest_size=8
    ).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    return unsigned - 2**64 if unsigned >= 2**63 else unsigned


def verify_project_name(index: int) -> str:
    """The compose project name for slot ``index``.

    Named after the SLOT and never the run: a run-scoped name would make every
    orphaned stack a distinct project that nobody ever revisits, so the disk
    fills with debris no one is looking for. A slot-scoped name guarantees the
    next acquirer meets — and cleans up — exactly one predecessor.
    """
    return f"verify-slot-{index}"


@dataclass(frozen=True)
class VerifySlot:
    """A held verification slot."""

    index: int

    @property
    def project(self) -> str:
        """The compose project name this slot owns."""
        return verify_project_name(self.index)


class _FallbackSlots:
    """In-process slot registry for the SQLite test path."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._held: set[int] = set()

    async def take(self, index: int) -> bool:
        async with self._guard:
            if index in self._held:
                return False
            self._held.add(index)
            return True

    async def give_back(self, index: int) -> None:
        async with self._guard:
            self._held.discard(index)


_FALLBACK: Final[_FallbackSlots] = _FallbackSlots()


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def _try_take(session: AsyncSession, index: int) -> bool:
    if _is_postgres(session):
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": verify_slot_key(index)}
        )
        return bool(result.scalar())
    return await _FALLBACK.take(index)


async def _give_back(session: AsyncSession, index: int) -> None:
    if _is_postgres(session):
        try:
            await session.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": verify_slot_key(index)}
            )
        except Exception:  # noqa: BLE001 — a dead session already freed it; that is the design
            logger.debug("verify_slot_unlock_skipped", slot=index)
        return
    await _FALLBACK.give_back(index)


@asynccontextmanager
async def acquire_verify_slot(
    session: AsyncSession, *, slots: int = DEFAULT_VERIFY_SLOTS
) -> AsyncIterator[VerifySlot | None]:
    """Hold a free verification slot, or ``None`` when all ``slots`` are taken.

    Exhaustion yields ``None`` rather than queueing or overrunning: the bound
    exists because the disk is finite, so the honest answer to "no capacity" is
    to not start a stack. The caller decides what to tell the founder.

    ``session`` must be the caller's own connection — the lock lives and dies
    with it, which is precisely what frees the slot when a run's process is
    killed mid-verification.
    """
    held: int | None = None
    for index in range(max(0, slots)):
        if await _try_take(session, index):
            held = index
            break
    if held is None:
        logger.info("verify_slot_unavailable", slots=slots)
        yield None
        return
    logger.info("verify_slot_acquired", slot=held, project=verify_project_name(held))
    try:
        yield VerifySlot(index=held)
    finally:
        await _give_back(session, held)
        logger.debug("verify_slot_released", slot=held)


async def load_workspace_verify_slots(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """The workspace's concurrent-verification budget (``workspaces.verify_stack_slots``).

    Best-effort: a missing workspace / unreadable column yields the default
    rather than breaking a run. Never returns < 0 — a negative budget would
    silently mean "no verification ever" instead of a loud misconfiguration.
    """
    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415 — cross-domain, local

    try:
        value = await session.scalar(
            select(WorkspaceRow.verify_stack_slots).where(WorkspaceRow.id == workspace_id)
        )
    except Exception:  # noqa: BLE001 — budget lookup must never break the run
        logger.warning("verify_slots_lookup_failed", workspace_id=str(workspace_id), exc_info=True)
        return DEFAULT_VERIFY_SLOTS
    if value is None:
        return DEFAULT_VERIFY_SLOTS
    return max(0, int(value))


__all__ = [
    "DEFAULT_VERIFY_SLOTS",
    "VerifySlot",
    "acquire_verify_slot",
    "load_workspace_verify_slots",
    "verify_project_name",
    "verify_slot_key",
]
