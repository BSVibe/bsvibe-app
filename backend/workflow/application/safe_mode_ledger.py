"""The requirements-ledger verdict carried on a Safe Mode queue item.

The founder approves a held delivery without opening its report, so the items the gate
could not confirm (``unclear``) or the agent deferred must be on the card itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.requirements_ledger import ledger_rows_for_runs
from backend.workflow.domain.requirements import ledger_counts

_ATTENTION_STATUSES = frozenset({"unclear", "deferred"})


class LedgerAttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str


class RunLedger(BaseModel):
    """``counts`` is the report's ``ledger_counts`` (``{}`` when the run has no ledger)."""

    model_config = ConfigDict(extra="forbid")

    counts: dict[str, int] = {}
    attention: list[LedgerAttentionItem] = []


async def ledgers_for_runs(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, RunLedger]:
    """Ledger summary per run, read in one query; a run without a ledger is absent."""
    rows_by_run = await ledger_rows_for_runs(
        session, {r for r in run_ids if r is not None}, workspace_id=workspace_id
    )
    return {
        run_id: RunLedger(
            counts=ledger_counts(rows),
            attention=[
                LedgerAttentionItem(id=str(r.get("id", "")), text=str(r.get("text", "")))
                for r in rows
                if r.get("status") in _ATTENTION_STATUSES
            ],
        )
        for run_id, rows in rows_by_run.items()
    }


__all__ = ["LedgerAttentionItem", "RunLedger", "ledgers_for_runs"]
