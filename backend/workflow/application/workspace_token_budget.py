"""게이트 1, ceiling 2 — the per-WORKSPACE cumulative token budget (#930 part 1).

The per-run ceiling (:mod:`backend.workflow.application.token_budget`) bounds
ONE run. Nothing bounded a workspace: three concurrent runs, restarted forever,
sits inside ``max_concurrent_runs`` and is unbounded in tokens. #953 sealed the
meter leaks, so ``execution_runs.usage_*`` now totals act + frame + verify +
tick and is finally a number worth budgeting against.

**Why this is a separate module from the per-run ceiling.** They are two halves
of one subject and would read better together, but
``account_and_enforce_token_cap`` is typed against ``RunOrchestrator``, so
``token_budget`` carries a ``TYPE_CHECKING`` import of ``agent_loop`` — and
import-linter counts ``TYPE_CHECKING`` edges. Any importer of ``token_budget``
therefore inherits the whole loop graph (``connector_actions`` →
``backend.connectors``/``backend.router``, ``emit_deliverable`` →
``backend.api``). This rule's callers include ``backend.mcp``, which is
contractually forbidden all three. Measured, not guessed: putting these
functions in ``token_budget`` broke the "MCP context depends only on Identity +
Workflow + Knowledge + common" contract three ways. The seam is the contract's,
not a preference.

**Neither ceiling is a price lever.** ``token_budget`` used to justify that with
"BYO-key means the spend is the founder's own provider bill". The founder decided
on 2026-09-14 that BSVibe opens to public signup, which breaks the BYO-key
premise — but the conclusion is unchanged and this module states it in its own
words rather than inheriting the old sentence: this is a SAFETY quota.
``workspaces.max_concurrent_runs`` is the free plan's price lever and stays the
only one; billing is #928. A quota that quietly became a price lever would be
sold without anyone deciding to sell it.

**Why ADMISSION and not mid-run.** The per-run ceiling has to be mid-run — a
single run can blow it without ever passing a door again. A cumulative budget
cannot: crossing it says the workspace is out of budget for the PERIOD, which is
a statement about what it may START, not about the run in flight. Stopping that
run mid-work would throw away spend already made and buy nothing, which is
exactly the argument #953 records for accruing but NOT enforcing on the verify
turns. Admission also gives the refusal a screen to appear on — the reason
``run_caps`` gates the founder-submission door and deliberately not the worker
that mints the run.

**The gap ``run_caps`` documents applies here too**, for its reasons, and is not
re-argued: runs born WITHOUT a founder submission (a connector webhook, a
schedule tick, a multi-step frame spawning its next step) never pass through
admission. They are *counted* — they spend the budget the founder's next
submission is measured against — but they are not refused. Dropping an inbound
webhook loses the founder's event with nowhere to say so. If that becomes a real
leak, the gate belongs in intake, with a refusal the founder can see.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import DEFAULT_MONTHLY_TOKEN_BUDGET, WorkspaceRow
from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

#: The machine code the REST refusal carries. The PWA writes its own localized
#: sentence from it — an English string would land verbatim on a KO surface.
WORKSPACE_TOKEN_BUDGET_CODE = "workspace_token_budget_reached"  # noqa: S105 — a code, not a secret


class TokenBudgetReached(Exception):
    """Raised when a workspace has spent its whole budget for the period.

    Carries the numbers rather than a sentence — the same choice
    :class:`~backend.workflow.application.run_caps.RunCapReached` makes: each
    surface writes its own (localized) message, and the PWA must not hardcode a
    limit that differs per workspace. ``window_start`` is in there because "when
    do I get it back" is the only thing a founder can act on.
    """

    def __init__(self, *, limit: int, used: int, window_start: datetime) -> None:
        super().__init__(f"workspace spent {used} of {limit} tokens since {window_start}")
        self.limit = limit
        self.used = used
        self.window_start = window_start


def budget_window_start(now: datetime | None = None) -> datetime:
    """The instant the current budget period opened — this month's 1st, 00:00 UTC.

    **Why a calendar month and not a lifetime total.** A lifetime total can only
    ever be exhausted: once a workspace has spent its number it is dead forever
    and only an operator UPDATE brings it back. That is a fuse, not a budget.

    **Why a calendar month and not a rolling window.** Both are indexable and
    both bound the spend. The month wins on the two things that matter here: a
    refusal has to be actionable, and "your budget reopens on the 1st, UTC" is an
    answer this function gives in one line, where a rolling window's "when am I
    unblocked" needs a second query over individual runs. And #928 (billing) will
    have a billing period — a quota window already shaped like one does not have
    to be re-cut when it arrives.

    Computed in Python and passed as a bound parameter, NOT as ``date_trunc`` —
    that function does not exist on the SQLite test tier, and a predicate that
    runs on only one of the two tiers is a predicate only one tier tests.

    ⚠️ ``execution_runs.created_at``'s ORM default is a naive ``datetime.now()``
    (server-local), while several call sites pass an aware UTC value. SQLite
    stores a naive value verbatim; asyncpg localizes it into the ``timestamptz``
    column (measured on a probe PG: a −9h shift for a KST server). So rows
    written with the ORM default sit at the server's UTC offset from where a
    reader would put them, and the month boundary is exact only to within that
    offset. That inconsistency predates this feature and is NOT fixed here — it
    is recorded so nobody reads the boundary as tighter than it is. Nothing in
    the budget turns on an hour.
    """
    ref = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime(ref.year, ref.month, 1, tzinfo=UTC)


async def load_workspace_token_budget(session: AsyncSession, workspace_id: uuid.UUID) -> int | None:
    """This workspace's per-period token budget. ``None`` = unlimited.

    A missing workspace row falls back to the default rather than to
    "unlimited", for the reason ``run_caps.load_run_cap`` states: an unknown
    workspace must not be the cheapest way past the gate. A budget of ``0`` is
    honoured as written (it refuses everything) — rewriting it to the default
    would hide a deliberate suspension.
    """
    row = await session.get(WorkspaceRow, workspace_id)
    if row is None:
        logger.warning("token_budget_workspace_missing", workspace_id=str(workspace_id))
        return DEFAULT_MONTHLY_TOKEN_BUDGET
    return row.monthly_token_budget


async def sum_workspace_tokens_since(
    session: AsyncSession, workspace_id: uuid.UUID, since: datetime
) -> int:
    """Total LLM tokens this workspace's runs burned at or after ``since``.

    Prompt + completion, across EVERY run in the window regardless of status — a
    shipped run does not refund the provider bill. That is the deliberate
    difference from ``run_caps.count_held_runs``, which excludes terminal runs
    because a finished run holds nothing.

    Served by ``ix_execution_runs_ws_created`` — neither existing composite on
    ``execution_runs`` covers ``(workspace_id, created_at)``.
    """
    total = await session.scalar(
        select(
            func.coalesce(
                func.sum(ExecutionRun.usage_prompt_tokens + ExecutionRun.usage_completion_tokens),
                0,
            )
        ).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.created_at >= since,
        )
    )
    return int(total or 0)


async def enforce_workspace_token_budget(
    session: AsyncSession, *, workspace_id: uuid.UUID, now: datetime | None = None
) -> None:
    """Raise :class:`TokenBudgetReached` when this period's budget is spent.

    Called at the founder-submission door, beside ``enforce_run_cap`` and AFTER
    it: a workspace that is both at its run cap and over its budget keeps
    answering ``run_cap_reached``, the refusal the PWA already renders with a
    "ship or discard a run" hint. Adding a second gate must not silently relabel
    an existing one.
    """
    limit = await load_workspace_token_budget(session, workspace_id)
    if limit is None:
        return
    window_start = budget_window_start(now)
    used = await sum_workspace_tokens_since(session, workspace_id, window_start)
    if used < limit:
        return
    logger.info(
        "workspace_token_budget_reached",
        workspace_id=str(workspace_id),
        limit=limit,
        used=used,
        window_start=window_start.isoformat(),
    )
    raise TokenBudgetReached(limit=limit, used=used, window_start=window_start)


__all__ = [
    "DEFAULT_MONTHLY_TOKEN_BUDGET",
    "WORKSPACE_TOKEN_BUDGET_CODE",
    "TokenBudgetReached",
    "budget_window_start",
    "enforce_workspace_token_budget",
    "load_workspace_token_budget",
    "sum_workspace_tokens_since",
]
