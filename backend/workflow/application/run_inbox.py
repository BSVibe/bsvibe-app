"""A run's inbox — what the founder sends a run while it may be running, applied by the drive.

Intake used to write what it routed (an issue edit, #1268) straight onto ``run.payload``. The
drive holds its own copy of the run for the whole drive and writes it back as it goes, so a
routed edit was written over; and the drive read routed edits only when a drive started, so a
mid-drive edit was never told anyway (prod #1293, run 94e04ab3: two edits routed, both gone
by the verdict).

So intake only APPENDS a row here — an ``execution_run_activities`` row, insert-only, never
in conflict with anything — and the drive, the payload's one writer, applies the rows it has
not applied yet: at its start and between every turn (:func:`apply_inbox`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.pr_review import COMMENT_RECEIVED_AT_KEY, record_comment
from backend.workflow.domain.requirements import REQUIREMENTS_KEY, refresh_ledger
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity

logger = structlog.get_logger(__name__)

INBOX_ACTIVITY = "founder_inbox"
#: Ids of the inbox rows the drive has applied — on the run payload, written by the drive only.
APPLIED_KEY = "inbox_applied"

ISSUE_EDIT = "issue_edit"
ISSUE_COMMENT = "issue_comment"
PR_COMMENT = "pr_comment"


async def post_to_inbox(
    session: AsyncSession, run: ExecutionRun, kind: str, data: Mapping[str, Any]
) -> None:
    """Append one message for ``run``. Never touches the run row."""
    session.add(
        ExecutionRunActivity(
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type=INBOX_ACTIVITY,
            payload={"kind": kind, **data},
        )
    )
    await session.flush()


def _live(ledger: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [i for i in ledger if not i.get("withdrawn")]


def _refresh(
    payload: dict[str, Any], new_ledger: Sequence[Mapping[str, Any]]
) -> tuple[list[str], list[str]]:
    """Refresh the payload's ledger to ``new_ledger``; ``(added, withdrawn)`` as "id text"."""
    old = payload.get(REQUIREMENTS_KEY)
    if not isinstance(old, list) or not old:
        return [], []
    refreshed = refresh_ledger(old, new_ledger)
    old_ids = {str(i.get("id")) for i in old}
    was_withdrawn = {str(i.get("id")) for i in old if i.get("withdrawn")}
    added = [f"{i['id']} {i['text']}" for i in refreshed if str(i["id"]) not in old_ids]
    withdrawn = [
        f"{i['id']} {i['text']}"
        for i in refreshed
        if i.get("withdrawn") and str(i["id"]) not in was_withdrawn
    ]
    payload[REQUIREMENTS_KEY] = refreshed
    return added, withdrawn


def _apply_edit(payload: dict[str, Any], data: Mapping[str, Any]) -> str:
    added, withdrawn = _refresh(payload, data.get("ledger") or [])
    intent = str(data.get("intent_text") or "").strip()
    if intent:
        payload["intent_text"] = intent
    lines = [
        f"The founder edited the issue (#{data.get('issue_number')}) this run is working on. "
        "The edited text is now the request; where it differs from what you were doing, "
        "the edit wins.",
    ]
    if added:
        lines.append("New ledger items: " + "; ".join(added))
    if withdrawn:
        lines.append(
            "Items withdrawn (no longer asked — send no evidence for them): " + "; ".join(withdrawn)
        )
    if intent:
        lines.append(f"The issue now reads:\n\n{intent}")
    return "\n".join(lines)


def _apply_comment(payload: dict[str, Any], data: Mapping[str, Any]) -> str:
    # A comment ADDS: the live items stay as they are, the comment's checklist is appended.
    old = payload.get(REQUIREMENTS_KEY)
    items = data.get("ledger") or []
    added: list[str] = []
    if isinstance(old, list) and old and items:
        added, _ = _refresh(payload, [*_live(old), *items])
    lines = [
        f"{data.get('author') or 'The founder'} commented on the issue "
        f"(#{data.get('issue_number')}) this run is working on. Treat it as part of the request."
    ]
    if added:
        lines.append("New ledger items: " + "; ".join(added))
    lines.append(f"The comment:\n\n{data.get('text') or ''}")
    return "\n".join(lines)


def _apply_pr_comment(payload: dict[str, Any], data: Mapping[str, Any]) -> str:
    pr_number = int(data.get("pr_number") or 0)
    body = str(data.get("body") or "")
    author = str(data.get("author") or "")
    # ``resend`` is a founder's "fix it again": the comment is already on the record.
    if not data.get("resend"):
        try:
            received_at = datetime.fromisoformat(str(data.get("received_at")))
        except ValueError:
            received_at = datetime.now(tz=UTC)
        # The record stays after delivery, so a run that later fails can still be told apart
        # from one that never heard the reviewer.
        payload[COMMENT_RECEIVED_AT_KEY] = received_at.isoformat()
        record_comment(
            payload, pr_number=pr_number, body=body, author=author, received_at=received_at
        )
    return (
        f"{author or 'A reviewer'} left a review comment on your pull request "
        f"#{pr_number}:\n\n{body}\n\n"
        "Address it on the SAME branch so the result is pushed to the same pull request. "
        "Do not open a new pull request."
    )


_APPLY = {ISSUE_EDIT: _apply_edit, ISSUE_COMMENT: _apply_comment, PR_COMMENT: _apply_pr_comment}


async def apply_inbox(session: AsyncSession, run: ExecutionRun) -> list[dict[str, Any]]:
    """Apply the inbox rows not applied yet, in order; each is told once, as a user message."""
    payload = dict(run.payload or {})
    applied = {str(x) for x in payload.get(APPLIED_KEY) or []}
    rows = (
        await session.execute(
            select(ExecutionRunActivity.id, ExecutionRunActivity.payload)
            .where(
                ExecutionRunActivity.run_id == run.id,
                ExecutionRunActivity.activity_type == INBOX_ACTIVITY,
            )
            .order_by(ExecutionRunActivity.created_at, ExecutionRunActivity.id)
        )
    ).all()
    fresh = [(str(row_id), data) for row_id, data in rows if str(row_id) not in applied]
    if not fresh:
        return []
    messages: list[dict[str, Any]] = []
    for row_id, data in fresh:
        applied.add(row_id)  # told once — and a row that cannot be read is not retried
        apply = _APPLY.get(str(data.get("kind"))) if isinstance(data, Mapping) else None
        if apply is None:
            continue
        # On a copy: a row that cannot be read leaves no half-applied change, and never
        # stops the drive this runs inside (start, every turn, before every verification).
        trial = dict(payload)
        try:
            content = apply(trial, data)
        except Exception:  # noqa: BLE001 — skipped, logged; the rows around it still apply
            logger.warning(
                "run_inbox_row_skipped", run_id=str(run.id), row_id=row_id, exc_info=True
            )
            continue
        payload = trial
        messages.append({"role": "user", "content": content})
    payload[APPLIED_KEY] = sorted(applied)
    run.payload = payload
    await session.flush()
    return messages


__all__ = [
    "APPLIED_KEY",
    "INBOX_ACTIVITY",
    "ISSUE_COMMENT",
    "ISSUE_EDIT",
    "PR_COMMENT",
    "apply_inbox",
    "post_to_inbox",
]
