"""Concurrency slots for the disposable verification stack.

The full-surface verification design stands the product's whole stack up per
run on the SAME machine as prod. Two constraints collide:

* **Disk.** A full disk on this Mac Mini is an unrecoverable brick, so the
  number of simultaneous stacks must be bounded — and the bound is a
  per-workspace DB setting, not a constant, because it is a plan tier
  ("N concurrent verifications") rather than an implementation detail.
* **Orphans.** A run whose process dies never runs its ``finally``, so its
  stack survives. A naive counter would then leak that slot FOREVER and the
  limit becomes a deadlock — the feature turns itself off.

The resolution: a slot is held by a **PG advisory lock**, which is session
scoped and therefore released by the database when the holder's connection
dies. And the compose project is named after the SLOT, not the run — so
whoever acquires the slot next tears down whatever the previous holder left.
**Reclaiming the slot IS reclaiming the stack**; no reaper worker is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.infrastructure.db import ExecutionBase
from tests._support import db_engine, use_real_pg

pytestmark = pytest.mark.asyncio

#: The slot lease is a PG advisory lock. SQLite has no equivalent, and the
#: property under test — the DATABASE releasing the lock when the holder's
#: connection dies — cannot be simulated in-process.
_needs_pg = pytest.mark.skipif(
    not use_real_pg(), reason="advisory-lock slot leases are a PostgreSQL primitive"
)


@asynccontextmanager
async def _two_sessions() -> AsyncIterator[tuple[AsyncSession, AsyncSession]]:
    """Two INDEPENDENT connections — one lease holder each."""
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as a, sf() as b:
            yield a, b


async def test_project_name_is_derived_from_the_slot_not_the_run() -> None:
    """A run-scoped name would make every orphan a distinct project nobody ever
    revisits. A slot-scoped name means the next acquirer collides with — and so
    cleans up — exactly one predecessor."""
    from backend.workflow.infrastructure.verify_slots import verify_project_name

    assert verify_project_name(0) == "verify-slot-0"
    assert verify_project_name(3) == "verify-slot-3"


async def test_slot_keys_are_distinct_and_stable() -> None:
    from backend.workflow.infrastructure.verify_slots import verify_slot_key

    keys = {verify_slot_key(i) for i in range(8)}
    assert len(keys) == 8, "two slots hashing together would silently halve capacity"
    assert verify_slot_key(0) == verify_slot_key(0), "keys must be stable across calls"
    assert all(-(2**63) <= k < 2**63 for k in keys), "must fit pg_try_advisory_lock's bigint"


async def test_slot_key_space_is_disjoint_from_the_other_leases() -> None:
    """A collision with the workspace-promote key space would let one subsystem
    silently refuse another's lock."""
    from backend.workflow.infrastructure.lease import workspace_promote_key
    from backend.workflow.infrastructure.verify_slots import verify_slot_key

    wid = uuid.UUID(int=0)
    assert verify_slot_key(0) != workspace_promote_key(wid)


@_needs_pg
async def test_acquire_returns_a_slot_and_releases_it() -> None:
    from backend.workflow.infrastructure.verify_slots import acquire_verify_slot

    async with _two_sessions() as (a, _b), acquire_verify_slot(a, slots=2) as slot:
        assert slot is not None
        assert slot.index == 0
        assert slot.project == "verify-slot-0"


@_needs_pg
async def test_a_second_holder_gets_the_next_free_slot() -> None:
    from backend.workflow.infrastructure.verify_slots import acquire_verify_slot

    async with _two_sessions() as (a, b):
        async with acquire_verify_slot(a, slots=2) as first:
            assert first is not None and first.index == 0
            async with acquire_verify_slot(b, slots=2) as second:
                assert second is not None and second.index == 1


@_needs_pg
async def test_no_slot_when_all_are_taken() -> None:
    """Capacity exhaustion is a REFUSAL, not a queue and not an overrun — the
    disk bound is the point."""
    from backend.workflow.infrastructure.verify_slots import acquire_verify_slot

    async with _two_sessions() as (a, b):
        async with acquire_verify_slot(a, slots=1) as first:
            assert first is not None
            async with acquire_verify_slot(b, slots=1) as second:
                assert second is None


@_needs_pg
async def test_slot_frees_when_the_holder_connection_dies() -> None:
    """The orphan property, stated directly: no `finally`, no TTL, no reaper —
    the database releases a session-scoped advisory lock when the session goes.
    """
    from backend.workflow.infrastructure.verify_slots import acquire_verify_slot

    async with _two_sessions() as (a, b):
        ctx = acquire_verify_slot(a, slots=1)
        got = await ctx.__aenter__()
        assert got is not None
        # The holder's process dies: the connection goes WITHOUT the context
        # manager ever unwinding.
        await a.close()

        async with acquire_verify_slot(b, slots=1) as after:
            assert after is not None, "a dead holder must not hold its slot forever"
            assert after.index == 0


# ── the budget is a workspace SETTING, not a server constant ────────────────
# It is a plan tier ("N concurrent verifications"), so it lives in the DB where
# a founder / billing tier can move it without a redeploy.


async def test_budget_defaults_when_the_workspace_is_unknown() -> None:
    from backend.workflow.infrastructure.verify_slots import (
        DEFAULT_VERIFY_SLOTS,
        load_workspace_verify_slots,
    )

    async with _two_sessions() as (a, _b):
        assert await load_workspace_verify_slots(a, uuid.uuid4()) == DEFAULT_VERIFY_SLOTS


async def test_budget_is_read_from_the_workspace_row() -> None:
    from backend.identity.workspaces_db import WorkspaceRow
    from backend.workflow.infrastructure.verify_slots import load_workspace_verify_slots

    async with _two_sessions() as (a, _b):
        wid = uuid.uuid4()
        a.add(WorkspaceRow(id=wid, name="ws", verify_stack_slots=3))
        await a.commit()

        assert await load_workspace_verify_slots(a, wid) == 3
