"""The requirements-ledger verdict carried on a Safe Mode queue item.

The founder approves a held delivery without opening its report, so the items the gate
could not confirm (``unclear``) or the agent deferred must be on the card itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.requirements_ledger import ledger_rows_for_runs
from backend.workflow.domain.artifact_review import SEVERITIES
from backend.workflow.domain.requirements import ledger_counts

_ATTENTION_STATUSES = frozenset({"unclear", "deferred"})
_REVIEW_SHOWN = 5


class LedgerAttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str


class ReviewFindingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str
    file: str | None = None
    line: int | None = None
    claim: str


class RunLedger(BaseModel):
    """``counts`` is the report's ``ledger_counts`` (``{}`` when the run has no ledger).

    ``review_*`` is the independent review of the run's last PASSED round (report-only):
    ``review_status`` is ``done`` / ``unavailable`` / ``none``.
    """

    model_config = ConfigDict(extra="forbid")

    counts: dict[str, int] = {}
    attention: list[LedgerAttentionItem] = []
    #: Items judged ``done`` BECAUSE they followed a founder correction (denial reason).
    corrected: list[LedgerAttentionItem] = []
    review_status: str = "none"
    review_counts: dict[str, int] = {}
    review_findings: list[ReviewFindingItem] = []


_Review = tuple[str, dict[str, int], list[ReviewFindingItem]]


def _review_summary(result: Any) -> _Review:
    review = result.get("review") if isinstance(result, dict) else None
    if not isinstance(review, dict):
        return "none", {}, []
    if review.get("status") != "done":
        return "unavailable", {}, []
    findings = [f for f in review.get("findings") or [] if isinstance(f, dict)]
    counts = {sev: sum(1 for f in findings if f.get("severity") == sev) for sev in SEVERITIES}
    rank = {sev: i for i, sev in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: rank.get(str(f.get("severity")), len(rank)))
    top = [
        ReviewFindingItem(
            severity=str(f.get("severity")),
            file=f["file"] if isinstance(f.get("file"), str) else None,
            line=f["line"] if isinstance(f.get("line"), int) else None,
            claim=str(f.get("claim", "")),
        )
        for f in findings[:_REVIEW_SHOWN]
    ]
    return "done", counts, top


async def _reviews_for_runs(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_ids: set[uuid.UUID]
) -> dict[uuid.UUID, _Review]:
    """Each run's LAST passed round's review (a run whose last passed round has none is absent)."""
    from backend.workflow.infrastructure.db import (  # noqa: PLC0415
        VerificationOutcome,
        VerificationResult,
    )

    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(VerificationResult.run_id, VerificationResult.result)
            .where(
                VerificationResult.run_id.in_(run_ids),
                VerificationResult.workspace_id == workspace_id,
                VerificationResult.outcome == VerificationOutcome.PASSED,
            )
            .order_by(VerificationResult.created_at.desc(), VerificationResult.id.desc())
        )
    ).all()
    found: dict[uuid.UUID, _Review] = {}
    for run_id, result in rows:
        if run_id not in found:
            found[run_id] = _review_summary(result)
    return {r: v for r, v in found.items() if v[0] != "none"}


async def ledgers_for_runs(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, RunLedger]:
    """Ledger summary per run, read in one query; a run without a ledger is absent."""
    ids = {r for r in run_ids if r is not None}
    rows_by_run = await ledger_rows_for_runs(session, ids, workspace_id=workspace_id)
    reviews = await _reviews_for_runs(session, workspace_id=workspace_id, run_ids=ids)
    out: dict[uuid.UUID, RunLedger] = {}
    for run_id in rows_by_run.keys() | reviews.keys():
        rows = rows_by_run.get(run_id, [])
        status, counts, findings = reviews.get(run_id, ("none", {}, []))
        out[run_id] = RunLedger(
            counts=ledger_counts(rows) if rows else {},
            attention=[
                LedgerAttentionItem(id=str(r.get("id", "")), text=str(r.get("text", "")))
                for r in rows
                if r.get("status") in _ATTENTION_STATUSES
            ],
            corrected=[
                LedgerAttentionItem(id=str(r.get("id", "")), text=str(r.get("text", "")))
                for r in rows
                if r.get("status") == "done" and r.get("founder_correction")
            ],
            review_status=status,
            review_counts=counts,
            review_findings=findings,
        )
    return out


__all__ = ["LedgerAttentionItem", "ReviewFindingItem", "RunLedger", "ledgers_for_runs"]
