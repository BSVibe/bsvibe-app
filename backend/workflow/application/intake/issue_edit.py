"""Route an edit to an issue whose run is still in flight back to THAT run (#1268).

``issues.edited`` used to land as a new request: a second run — and a second PR — for the
same issue, while the first run kept the body (and the requirements ledger) it started from.
Prod #1263 (2026-10-29): R3 was wrong, the issue was corrected, the run never saw it, deferred
R3, and its PR said ``Refs``.

The same shape as :mod:`.pr_comment`: run in the intake worker AFTER the binding filter and
the budget/run-cap gate, write the edit onto the run's payload, re-open a run that was waiting
for review. Two differences:

* **Who may edit.** An ``issues`` delivery carries the issue AUTHOR's association, never the
  editor's. So an edit is routed only when the editor IS the author and that author is OWNER /
  MEMBER / COLLABORATOR — or, since a private org member shows as CONTRIBUTOR, has write access
  to the repo (:mod:`.trust`). Anyone else's edit falls through to a new request, exactly as before —
  on a public repo a stranger does not get to rewrite a running job.
* **What changes.** The run's ``intent_text`` becomes the edited issue, and its ledger is
  refreshed (:func:`~backend.workflow.domain.requirements.refresh_ledger`: same item, same id;
  a removed item is ``withdrawn``; a new item gets the next id).
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
from backend.workflow.application.run_status import move_run_status
from backend.workflow.domain.requirements import REQUIREMENTS_KEY, build_ledger, refresh_ledger
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import RequestRow

logger = structlog.get_logger(__name__)

ISSUE_EDITS_KEY = "issue_edits"

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
    async with workspace_session_scope(session, workspace_id):
        run = await _run_for_issue(session, workspace_id, repo, number)
        if run is None:
            return None
        run_payload = dict(run.payload or {})
        added: list[str] = []
        withdrawn: list[str] = []
        old_ledger = run_payload.get(REQUIREMENTS_KEY)
        if isinstance(old_ledger, list) and old_ledger:
            refreshed = refresh_ledger(old_ledger, build_ledger(payload))
            old_ids = {str(i.get("id")) for i in old_ledger}
            was_withdrawn = {str(i.get("id")) for i in old_ledger if i.get("withdrawn")}
            added = [f"{i['id']} {i['text']}" for i in refreshed if str(i["id"]) not in old_ids]
            withdrawn = [
                f"{i['id']} {i['text']}"
                for i in refreshed
                if i.get("withdrawn") and str(i["id"]) not in was_withdrawn
            ]
            run_payload[REQUIREMENTS_KEY] = refreshed
        intent = str(payload.get("intent_text") or "").strip()
        if intent:
            run_payload["intent_text"] = intent
        edits = list(run_payload.get(ISSUE_EDITS_KEY) or [])
        edits.append(
            {"issue_number": number, "text": intent, "added": added, "withdrawn": withdrawn}
        )
        run_payload[ISSUE_EDITS_KEY] = edits
        run.payload = run_payload
        if run.status in _REOPEN:
            await move_run_status(
                session, run, RunStatus.OPEN, reason=f"issue #{number} was edited"
            )
        await session.flush()
    logger.info(
        "issue_edit_routed_to_run",
        run_id=str(run.id),
        repo=repo,
        issue_number=number,
        run_status=run.status.value,
        added=len(added),
        withdrawn=len(withdrawn),
    )
    return run.id


def issue_edit_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """The queued edits as turn context — and consumed: each is told once."""
    payload = dict(run.payload or {})
    edits = payload.get(ISSUE_EDITS_KEY)
    if not isinstance(edits, list) or not edits:
        return []
    messages: list[dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, Mapping):
            continue
        lines = [
            f"The founder edited the issue (#{edit.get('issue_number')}) this run is working on. "
            "The edited text is now the request; where it differs from what you were doing, "
            "the edit wins.",
        ]
        if edit.get("added"):
            lines.append("New ledger items: " + "; ".join(map(str, edit["added"])))
        if edit.get("withdrawn"):
            lines.append(
                "Items withdrawn (no longer asked — send no evidence for them): "
                + "; ".join(map(str, edit["withdrawn"]))
            )
        if edit.get("text"):
            lines.append(f"The issue now reads:\n\n{edit['text']}")
        messages.append({"role": "user", "content": "\n".join(lines)})
    payload.pop(ISSUE_EDITS_KEY, None)
    run.payload = payload
    return messages


__all__ = [
    "ISSUE_EDITS_KEY",
    "issue_edit_messages",
    "issue_edit_of",
    "route_issue_edit_to_run",
]
