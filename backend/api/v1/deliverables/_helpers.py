"""Shared verified-run lookup helpers (B4 trust-integrity).

A run is "verified" ONLY when a real PASSED :class:`VerificationResult` row
exists — never inferred from a Deliverable existing. Both ``list_get`` and
``proof`` need this lookup; centralising here keeps the rule single-sourced.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import VerificationOutcome, VerificationResult


async def verified_run_ids(
    session: AsyncSession, workspace_id: uuid.UUID, run_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of ``run_ids`` that have at least one PASSED VerificationResult.

    B4 defense-in-depth: a run is "verified" ONLY when a real PASSED
    :class:`VerificationResult` row exists — never inferred from a Deliverable
    existing. One indexed query covers the whole listing page. An empty input
    short-circuits to an empty set (no needless query)."""
    if not run_ids:
        return set()
    stmt = select(VerificationResult.run_id).where(
        VerificationResult.workspace_id == workspace_id,
        VerificationResult.run_id.in_(run_ids),
        VerificationResult.outcome == VerificationOutcome.PASSED,
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())


async def run_is_verified(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    """True iff the run has at least one PASSED VerificationResult (single row)."""
    return bool(await verified_run_ids(session, workspace_id, {run_id}))
