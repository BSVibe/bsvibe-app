"""Verification slots are TWO limits, not one — 감사 §Ⅱ verify slot.

``workspaces.verify_stack_slots`` is documented, at the column itself, as a
**PLAN TIER** ("N concurrent verifications") — "it lives here, per workspace,
rather than as a server constant". But the mechanism read it as the size of a
**machine-global** pool: ``acquire_verify_slot(slots=<the workspace's value>)``
looped ``range(slots)`` over ``verify_slot_key(index)``, and that key hashes the
index ALONE.

So one workspace raising its own tier to 5 raised the CONCURRENCY OF THE WHOLE
MACHINE to 5 — oversubscribing the one resource the bound exists to protect (a
full disk on this Mac Mini is an unrecoverable brick) and starving every other
tenant, whose slot 0 is the same lock. Today every prod workspace sits at 1, so
nothing misbehaves yet; the defect is latent and multi-tenant.

**Why the audit's remedy ("put a workspace axis in the slot key") is wrong.**
``verify_project_name(index)`` — the compose project — is global too, and that is
load-bearing: "the next acquirer of slot i collides with exactly one
predecessor's leftovers, and tears them down before starting". Give two
workspaces their own slot 0 and both own compose project ``verify-slot-0``, so
each tears down the OTHER's live stack. Put the axis in the project name as well
and N tenants × N slots land on one disk — defeating the primary constraint.

**The shape that works: two layers over the same primitive.**

1. A **tenant ticket** (``verify_tenant_ticket_key(workspace_id, i)``) enforces
   the plan tier: a workspace may hold at most its own ``slots``.
2. A **global slot** (``verify_slot_key(i)``, unchanged) enforces the disk bound
   and keeps owning the compose project name.

Both are session-scoped advisory locks on the same connection, so the existing
"the database frees a dead holder's lock" property covers both, and orphan
reclamation is untouched.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from backend.workflow.infrastructure.verify_slots import (
    acquire_verify_slot,
    verify_project_name,
    verify_slot_key,
    verify_tenant_ticket_key,
)

from .._support import memory_session


@asynccontextmanager
async def _sessions(n: int):
    """``n`` independent sessions — one per would-be concurrent holder."""
    async with memory_session() as a:
        if n == 1:
            yield [a]
            return
        async with memory_session() as b:
            if n == 2:
                yield [a, b]
                return
            async with memory_session() as c:
                yield [a, b, c]


# ── The ticket key space ─────────────────────────────────────────────────────


def test_the_ticket_key_carries_the_workspace() -> None:
    """Same index, different workspace ⇒ different key.

    This is the axis the slot key deliberately does NOT have. Without it a
    per-workspace tier cannot be expressed at all.
    """
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    assert verify_tenant_ticket_key(ws_a, 0) != verify_tenant_ticket_key(ws_b, 0)
    assert verify_tenant_ticket_key(ws_a, 0) != verify_tenant_ticket_key(ws_a, 1)
    # Stable across calls — a key that moved would free a lock nobody released.
    assert verify_tenant_ticket_key(ws_a, 0) == verify_tenant_ticket_key(ws_a, 0)


def test_the_ticket_key_space_is_disjoint_from_the_slot_key_space() -> None:
    """Two subsystems hashing onto the same bigint would let one silently refuse
    the other's acquire — the reason the module salts its keys at all."""
    ws = uuid.uuid4()
    tickets = {verify_tenant_ticket_key(ws, i) for i in range(64)}
    slots = {verify_slot_key(i) for i in range(64)}
    assert tickets.isdisjoint(slots)


def test_the_slot_still_owns_the_compose_project() -> None:
    """The orphan-reclamation mechanism must be untouched: the project name is
    named after the GLOBAL slot, never the tenant."""
    ws = uuid.uuid4()
    assert verify_project_name(0) == "verify-slot-0"
    # The ticket is a tier ticket only — it names no stack.
    assert str(ws) not in verify_project_name(0)


# ── Layer 1: a workspace cannot exceed its own tier ──────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_cannot_exceed_its_own_tier() -> None:
    """Machine has room, but this tenant's plan does not."""
    ws = uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws, slots=1, total_slots=4) as first:
            assert first is not None
            async with acquire_verify_slot(b, workspace_id=ws, slots=1, total_slots=4) as second:
                assert second is None, "the tier is the binding limit here"


@pytest.mark.asyncio
async def test_a_higher_tier_grants_more_of_the_pool() -> None:
    """The tier is a real budget, not just a refusal."""
    ws = uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws, slots=2, total_slots=4) as first:
            assert first is not None
            async with acquire_verify_slot(b, workspace_id=ws, slots=2, total_slots=4) as second:
                assert second is not None
                assert first.index != second.index, "two holders must own distinct stacks"


# ── Layer 2: the machine-wide disk bound holds regardless of tier ────────────


@pytest.mark.asyncio
async def test_the_disk_bound_holds_even_when_the_tier_is_higher() -> None:
    """THE fix. Before this, a workspace's own tier WAS the machine's limit.

    A tenant setting ``verify_stack_slots=5`` on a box that can hold one stack
    used to get five — the disk bound was whatever the last caller asked for.
    """
    ws = uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws, slots=5, total_slots=1) as first:
            assert first is not None
            async with acquire_verify_slot(b, workspace_id=ws, slots=5, total_slots=1) as second:
                assert second is None, "the disk bound must refuse past the machine's capacity"


@pytest.mark.asyncio
async def test_one_tenant_cannot_starve_another_past_the_machine_bound() -> None:
    """A second workspace is refused when the machine is full — but by the DISK
    bound, not by the first tenant's ticket."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws_a, slots=1, total_slots=1) as first:
            assert first is not None
            async with acquire_verify_slot(b, workspace_id=ws_b, slots=1, total_slots=1) as second:
                assert second is None


@pytest.mark.asyncio
async def test_two_workspaces_both_run_when_the_pool_allows() -> None:
    """The point of a global pool: tenants share it rather than serialize on
    slot 0. Before this, every workspace contended for the SAME key."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws_a, slots=1, total_slots=2) as first:
            assert first is not None
            async with acquire_verify_slot(b, workspace_id=ws_b, slots=1, total_slots=2) as second:
                assert second is not None
                assert first.index != second.index


# ── Release ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_layers_are_released_on_exit() -> None:
    """A ticket left behind would shrink the tenant's tier by one, forever."""
    ws = uuid.uuid4()
    async with _sessions(2) as (a, b):
        async with acquire_verify_slot(a, workspace_id=ws, slots=1, total_slots=1) as first:
            assert first is not None
        # Both layers freed — the very next acquire succeeds.
        async with acquire_verify_slot(b, workspace_id=ws, slots=1, total_slots=1) as again:
            assert again is not None


@pytest.mark.asyncio
async def test_a_full_machine_gives_the_ticket_back() -> None:
    """Refusal at layer 2 must not keep layer 1's ticket.

    Otherwise a tenant that merely ARRIVED while the disk was full would burn a
    slice of its own tier that nothing ever returns — the feature switching
    itself off, which is the exact failure the module's advisory-lock design
    exists to prevent.
    """
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    async with _sessions(3) as (a, b, c):
        async with acquire_verify_slot(a, workspace_id=ws_a, slots=1, total_slots=1) as holder:
            assert holder is not None
            async with acquire_verify_slot(b, workspace_id=ws_b, slots=1, total_slots=1) as denied:
                assert denied is None
        # ws_b was refused by the DISK, so its tier must be fully intact now.
        async with acquire_verify_slot(c, workspace_id=ws_b, slots=1, total_slots=1) as retry:
            assert retry is not None, "the refused attempt leaked ws_b's ticket"
