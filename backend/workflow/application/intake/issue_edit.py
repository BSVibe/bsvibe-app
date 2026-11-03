"""Route an edit to an issue whose run is still in flight back to THAT run (#1268).

``issues.edited`` used to land as a new request: a second run — and a second PR — for the
same issue, while the first run kept the body (and the requirements ledger) it started from.
Prod #1263 (2026-10-29): R3 was wrong, the issue was corrected, the run never saw it, deferred
R3, and its PR said ``Refs``.

Run in the intake worker AFTER the binding filter and the budget/run-cap gate. The edit — and,
the same way, a trusted COMMENT on the issue (which used to queue a second run for the same
issue: prod #1279, ``b9d0039c``) — goes to the run's inbox (:mod:`..run_inbox`), never onto the
run row: the drive holds its own copy of the run and wrote a routed edit over (prod #1293). A
run that was waiting for review is re-opened. Two rules:

* **Who may edit.** An ``issues`` delivery carries the issue AUTHOR's association, never the
  editor's. So an edit is routed only when the editor IS the author and that author is OWNER /
  MEMBER / COLLABORATOR — or, since a private org member shows as CONTRIBUTOR, has write access
  to the repo (:mod:`.trust`). Anyone else's edit falls through to a new request, exactly as before —
  on a public repo a stranger does not get to rewrite a running job.
* **What changes** (applied by the drive): an edit makes the edited issue the run's
  ``intent_text`` and refreshes its ledger (same item, same id; a removed item is
  ``withdrawn``; a new item gets the next id); a comment's checklist items join the ledger.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.trust import RepoPermission, is_trusted
from backend.workflow.application.run_inbox import ISSUE_COMMENT, ISSUE_EDIT, post_to_inbox
from backend.workflow.application.run_status import move_run_status
from backend.workflow.domain.requirements import (
    build_ledger,
    extract_requirements,
    number_requirements,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import RequestRow

logger = structlog.get_logger(__name__)

_APPEND_ONLY = (RunStatus.OPEN, RunStatus.RUNNING)
_REOPEN = (RunStatus.REVIEW_READY, RunStatus.FAILED)


def _issue(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    body = payload.get("body")
    issue = body.get("issue") if isinstance(body, Mapping) else None
    return issue if isinstance(issue, Mapping) else None


def issue_edit_of(payload: Mapping[str, Any]) -> tuple[str, int, str, str] | None:
    """``(repo, issue_number, author, author_association)`` when the issue's own author edited
    its body — whether that author may steer the run is :func:`.trust.is_trusted`'s call."""
    if payload.get("github_event") != "issues" or payload.get("action") != "edited":
        return None
    issue = _issue(payload)
    repo = payload.get("repo")
    number = issue.get("number") if issue is not None else None
    if issue is None or issue.get("pull_request") or not isinstance(number, int) or not repo:
        return None
    body = payload.get("body")
    sender = body.get("sender") if isinstance(body, Mapping) else None
    editor = str((sender or {}).get("login") or "") if isinstance(sender, Mapping) else ""
    author_map = issue.get("user")
    author = str(author_map.get("login") or "") if isinstance(author_map, Mapping) else ""
    association = str(issue.get("author_association") or "").upper()
    if not editor or editor != author:
        _not_routed(repo, number, editor, author, association)
        return None
    return str(repo), number, author, association


def _not_routed(repo: object, number: int, editor: str, author: str, association: str) -> None:
    logger.info(
        "issue_edit_not_routed_untrusted_editor",
        repo=repo,
        issue_number=number,
        editor=editor or None,
        author=author or None,
        author_association=association or None,
    )


def _same_issue(request_payload: Any, repo: str, number: int) -> bool:
    if not isinstance(request_payload, Mapping):
        return False
    if request_payload.get("github_event") not in {"issues", "issue_comment"}:
        return False
    if str(request_payload.get("repo") or "").lower() != repo.lower():
        return False
    issue = _issue(request_payload)
    return issue is not None and not issue.get("pull_request") and issue.get("number") == number


async def _run_for_issue(
    session: AsyncSession, workspace_id: uuid.UUID, repo: str, number: int
) -> ExecutionRun | None:
    rows = await session.execute(
        select(ExecutionRun, RequestRow.payload)
        .join(RequestRow, RequestRow.id == ExecutionRun.request_id)
        .where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.status.in_((*_APPEND_ONLY, *_REOPEN)),
        )
        .order_by(ExecutionRun.created_at.desc())
    )
    for run, request_payload in rows.all():
        if _same_issue(request_payload, repo, number):
            return run  # type: ignore[no-any-return]
    return None


async def _deliver(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    repo: str,
    number: int,
    kind: str,
    data: dict[str, Any],
) -> uuid.UUID | None:
    """Post ``data`` to the inbox of the unfinished run issue ``number`` started; its id."""
    async with workspace_session_scope(session, workspace_id):
        run = await _run_for_issue(session, workspace_id, repo, number)
        if run is None:
            return None
        await post_to_inbox(session, run, kind, {"issue_number": number, **data})
        if run.status in _REOPEN:
            await move_run_status(
                session, run, RunStatus.OPEN, reason=f"issue #{number}: {kind.replace('_', ' ')}"
            )
        await session.flush()
    logger.info(
        f"{kind}_routed_to_run",
        run_id=str(run.id),
        repo=repo,
        issue_number=number,
        run_status=run.status.value,
    )
    return run.id


async def route_issue_edit_to_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    payload: dict[str, Any],
    permission_of: RepoPermission | None = None,
) -> uuid.UUID | None:
    """Hand an issue edit to the run that issue started; its id, or ``None``.

    ``None`` — not an edit, an editor who is not the trusted author, no unfinished run for
    that issue — means the caller lands the delivery as a new request, as before. The caller
    commits."""
    parsed = issue_edit_of(payload)
    if parsed is None:
        return None
    repo, number, author, association = parsed
    if not await is_trusted(
        association, login=author, repo=repo, workspace_id=workspace_id, permission_of=permission_of
    ):
        _not_routed(repo, number, author, author, association)
        return None
    data = {
        "intent_text": str(payload.get("intent_text") or "").strip(),
        "ledger": build_ledger(payload),
    }
    return await _deliver(
        session, workspace_id=workspace_id, repo=repo, number=number, kind=ISSUE_EDIT, data=data
    )


async def route_issue_comment_to_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    payload: dict[str, Any],
    permission_of: RepoPermission | None = None,
) -> uuid.UUID | None:
    """Hand a trusted comment on an issue to the run that issue started; its id, or ``None``.

    ``None`` — not a comment on an issue (a PR's is :mod:`.pr_comment`'s), an untrusted
    author, no unfinished run — means the caller lands it as a new request, as before."""
    if payload.get("github_event") != "issue_comment":
        return None
    issue = _issue(payload)
    body = payload.get("body")
    comment = body.get("comment") if isinstance(body, Mapping) else None
    repo = str(payload.get("repo") or "")
    number = issue.get("number") if issue is not None else None
    if issue is None or issue.get("pull_request") or not isinstance(number, int) or not repo:
        return None
    if not isinstance(comment, Mapping):
        return None
    text = str(comment.get("body") or "").strip()
    user = comment.get("user")
    login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
    association = str(comment.get("author_association") or "").upper()
    if not text or not await is_trusted(
        association, login=login, repo=repo, workspace_id=workspace_id, permission_of=permission_of
    ):
        return None
    data = {
        "text": text,
        "author": login,
        "ledger": number_requirements(extract_requirements(text, source="comment")),
    }
    return await _deliver(
        session, workspace_id=workspace_id, repo=repo, number=number, kind=ISSUE_COMMENT, data=data
    )


__all__ = [
    "issue_edit_of",
    "route_issue_comment_to_run",
    "route_issue_edit_to_run",
]
