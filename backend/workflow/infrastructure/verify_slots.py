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

#: Domain salt for the TENANT TICKET key space — disjoint from the slot salt
#: above for the same reason that one is disjoint from promote/dispatch: two
#: subsystems hashing onto the same bigint would let one silently refuse the
#: other's acquire.
_VERIFY_TICKET_SALT: Final[bytes] = b"bsvibe.verify.ticket/"

#: Slot count when the workspace has not set one. Deliberately 1: the safe
#: default on a single founder machine is "one stack at a time".
DEFAULT_VERIFY_SLOTS: Final[int] = 1

#: Machine-wide stack count when the deployment has not set one.
#:
#: This is the DISK bound, and it is a different quantity from the per-workspace
#: tier above even though both default to 1. The tier says "how many concurrent
#: verifications this plan buys"; this says "how many stacks this box can hold
#: before a full disk bricks it". Conflating them is what let one workspace
#: raising its own tier raise the concurrency of the whole machine.
DEFAULT_VERIFY_SLOTS_TOTAL: Final[int] = 1


def verify_slot_key(index: int) -> int:
    """Stable signed-int64 advisory-lock key for verification slot ``index``."""
    digest = hashlib.blake2b(
        _VERIFY_SLOT_SALT + index.to_bytes(4, "big", signed=False), digest_size=8
    ).digest()
    unsigned = int.from_bytes(digest, byteorder="big", signed=False)
    return unsigned - 2**64 if unsigned >= 2**63 else unsigned


def verify_tenant_ticket_key(workspace_id: uuid.UUID, index: int) -> int:
    """Stable signed-int64 advisory-lock key for a workspace's tier ticket.

    The axis :func:`verify_slot_key` deliberately does not have. A ticket says
    "this workspace is using one of ITS OWN budgeted verifications"; it names no
    stack and owns no compose project — :func:`verify_project_name` stays keyed
    on the global slot so orphan reclamation keeps meeting exactly one
    predecessor.
    """
    digest = hashlib.blake2b(
        _VERIFY_TICKET_SALT + workspace_id.bytes + index.to_bytes(4, "big", signed=False),
        digest_size=8,
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
    """In-process lock registry for the SQLite test path.

    Keyed by the same computed advisory-lock key PostgreSQL uses, so both layers
    (global slot, tenant ticket) share one namespace here exactly as they share
    one on PG — a fallback keyed by bare index could not represent a ticket.
    """

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._held: set[int] = set()

    async def take(self, key: int) -> bool:
        async with self._guard:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    async def give_back(self, key: int) -> None:
        async with self._guard:
            self._held.discard(key)


_FALLBACK: Final[_FallbackSlots] = _FallbackSlots()


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def _try_take(session: AsyncSession, key: int) -> bool:
    if _is_postgres(session):
        result = await session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        return bool(result.scalar())
    return await _FALLBACK.take(key)


async def _give_back(session: AsyncSession, key: int) -> None:
    if _is_postgres(session):
        try:
            await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        except Exception:  # noqa: BLE001 — a dead session already freed it; that is the design
            logger.debug("verify_slot_unlock_skipped", key=key)
        return
    await _FALLBACK.give_back(key)


@asynccontextmanager
async def acquire_verify_slot(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    slots: int = DEFAULT_VERIFY_SLOTS,
    total_slots: int = DEFAULT_VERIFY_SLOTS_TOTAL,
) -> AsyncIterator[VerifySlot | None]:
    """Hold a free verification slot, or ``None`` when there is no capacity.

    TWO limits, both real, taken in order:

    1. ``slots`` — this workspace's PLAN TIER. Held as a tenant ticket
       (:func:`verify_tenant_ticket_key`), so a workspace can never run more
       concurrent verifications than its plan buys.
    2. ``total_slots`` — the MACHINE's disk bound. Held as the global slot
       (:func:`verify_slot_key`), which also names the compose project.

    They were one number before, and that was the defect: ``slots`` was looped
    over the GLOBAL key space, so a workspace raising its own tier raised the
    concurrency of the whole box — oversubscribing the finite disk the bound
    exists to protect, and starving every other tenant whose slot 0 is the same
    lock. ``workspace_id`` is required rather than defaulted precisely so a
    caller cannot silently fall back to the old single-axis behaviour.

    Exhaustion yields ``None`` rather than queueing or overrunning: the bound
    exists because the disk is finite, so the honest answer to "no capacity" is
    to not start a stack. The caller decides what to tell the founder.

    ``session`` must be the caller's own connection — the locks live and die
    with it, which is precisely what frees BOTH layers when a run's process is
    killed mid-verification.
    """
    ticket: int | None = None
    for index in range(max(0, slots)):
        key = verify_tenant_ticket_key(workspace_id, index)
        if await _try_take(session, key):
            ticket = key
            break
    if ticket is None:
        logger.info("verify_slot_tier_exhausted", workspace_id=str(workspace_id), slots=slots)
        yield None
        return

    held: int | None = None
    for index in range(max(0, total_slots)):
        if await _try_take(session, verify_slot_key(index)):
            held = index
            break
    if held is None:
        # Give the ticket back. Keeping it would burn a slice of this tenant's
        # tier for merely ARRIVING while the disk was full — the feature
        # switching itself off, which is the failure this design exists to avoid.
        await _give_back(session, ticket)
        logger.info(
            "verify_slot_unavailable", workspace_id=str(workspace_id), total_slots=total_slots
        )
        yield None
        return

    logger.info(
        "verify_slot_acquired",
        slot=held,
        project=verify_project_name(held),
        workspace_id=str(workspace_id),
    )
    try:
        yield VerifySlot(index=held)
    finally:
        await _give_back(session, verify_slot_key(held))
        await _give_back(session, ticket)
        logger.debug("verify_slot_released", slot=held)


@asynccontextmanager
async def open_slot_session() -> AsyncIterator[AsyncSession]:
    """A session whose CONNECTION exists only to hold a slot lock.

    Three properties, none optional:

    * **Its own connection.** ``pg_advisory_lock`` is session-scoped — scoped to
      the postgres backend, i.e. the connection. The run's own session commits
      repeatedly through verification (deliberately: #632/#686 — nothing may be
      held across the long external steps), and each commit returns its
      connection to the pool. A lock taken there would drift onto a connection
      someone else may be using, and the unlock would land somewhere else again.
    * **AUTOCOMMIT.** No transaction ever opens, so postgres'
      ``idle_in_transaction_session_timeout`` (120s) has nothing to kill while
      the stack takes its minutes. Being killed there would release the slot
      MID-verification and let the next acquirer tear the live stack down.
    * **Disposed on exit.** Closing the connection frees the lock even if the
      unlock statement never runs — which is the property the whole design rests
      on: a dead run's slot comes back without a reaper.
    """
    from backend.config import get_settings  # noqa: PLC0415
    from backend.data.engine import create_app_engine  # noqa: PLC0415

    engine = create_app_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            async with AsyncSession(bind=conn) as session:
                yield session
    finally:
        await engine.dispose()


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
    "DEFAULT_VERIFY_SLOTS_TOTAL",
    "load_workspace_verify_slots",
    "open_slot_session",
    "verify_project_name",
    "verify_slot_key",
    "verify_tenant_ticket_key",
]
