"""The requirements ledger as the PR sees it — the gate's last verdict, rendered, linked.

Split out of ``_github_pr.py`` to keep that file under the package's LOC cap.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.requirements_ledger import latest_ledger_rows
from backend.workflow.domain.requirements import ledger_closes, render_ledger_table

logger = structlog.get_logger(__name__)


async def _requirements_rows(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The ledger verdict of the run's newest verification (``[]`` = the run had none)."""
    from backend.data.rls import workspace_session_scope  # noqa: PLC0415

    async with session_factory() as session, workspace_session_scope(session, workspace_id):
        return await latest_ledger_rows(session, run_id)


async def _independent_review(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, workspace_id: uuid.UUID
) -> dict[str, Any] | None:
    """The independent review stored on the run's last PASSED verification (``None`` = none)."""
    from backend.data.rls import workspace_session_scope  # noqa: PLC0415
    from backend.workflow.infrastructure.db import (  # noqa: PLC0415
        VerificationOutcome,
        VerificationResult,
    )

    async with session_factory() as session, workspace_session_scope(session, workspace_id):
        result = (
            await session.execute(
                select(VerificationResult.result)
                .where(
                    VerificationResult.run_id == run_id,
                    VerificationResult.outcome == VerificationOutcome.PASSED,
                )
                .order_by(VerificationResult.created_at.desc(), VerificationResult.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    review = result.get("review") if isinstance(result, dict) else None
    return review if isinstance(review, dict) else None


async def with_stored_independent_review(
    session_factory: async_sessionmaker[AsyncSession],
    body: str,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> str:
    """:func:`with_independent_review` on the stored review; a failed read never blocks the PR."""
    try:
        review = await _independent_review(session_factory, run_id, workspace_id)
    except Exception:  # noqa: BLE001 — a missing review never blocks the PR
        logger.warning("github_delivery_review_failed", run_id=str(run_id), exc_info=True)
        return body
    return with_independent_review(body, review)


def with_independent_review(body: str, review: dict[str, Any] | None) -> str:
    """Append the "Independent review" section (report-only); no review, no section."""
    if review is None:
        return body
    if review.get("status") != "done":
        reason = str(review.get("reason") or "").strip() or "no reason recorded"
        return f"{body.strip()}\n\n## Independent review\n\nUnavailable: {reason}"
    lines = []
    for f in review.get("findings") or []:
        if not isinstance(f, dict):
            continue
        where = str(f.get("file") or "")
        if where and f.get("line") is not None:
            where = f"{where}:{f['line']}"
        place = f" `{where}`" if where else ""
        lines.append(
            f"- **{f.get('severity')}**{place} — {f.get('claim')} — {f.get('failure_scenario')}"
        )
    return f"{body.strip()}\n\n## Independent review\n\n" + ("\n".join(lines) or "No findings.")


def with_requirements_ledger(body: str, rows: list[dict[str, Any]]) -> str:
    """Append the ledger table (item · status · evidence) to the PR body."""
    if not rows:
        return body
    return f"{body.strip()}\n\n## Requirements ledger\n\n{render_ledger_table(rows)}"


def with_issue_link(body: str, *, source_issue: int | None, rows: list[dict[str, Any]]) -> str:
    """Link the PR to the issue that started the run.

    ``Closes #N`` closes the issue on merge, so it is written only when every owed item of
    the ledger is ``done`` (or the run had no ledger). Anything deferred, unclear or not shown
    gets ``Refs #N`` — a cross-link that closes nothing — and the issue stays open for what is
    left. A run not sourced from an issue is unchanged."""
    if source_issue is None:
        return body
    word = "Closes" if ledger_closes(rows) else "Refs"
    return f"{body}\n\n{word} #{source_issue}".strip()
