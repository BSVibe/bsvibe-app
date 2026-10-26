"""The requirements ledger as the PR sees it — the gate's last verdict, rendered, linked.

Split out of ``_github_pr.py`` to keep that file under the package's LOC cap.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.requirements_ledger import latest_ledger_rows
from backend.workflow.domain.requirements import ledger_closes, render_ledger_table


async def _requirements_rows(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The ledger verdict of the run's newest verification (``[]`` = the run had none)."""
    from backend.data.rls import workspace_session_scope  # noqa: PLC0415

    async with session_factory() as session, workspace_session_scope(session, workspace_id):
        return await latest_ledger_rows(session, run_id)


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
