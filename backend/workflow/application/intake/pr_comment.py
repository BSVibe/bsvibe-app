"""Route a trusted comment on a BSVibe-opened PR back to the run that opened it.

This runs in the intake worker, AFTER Receive's binding filter and the workspace
budget/run-cap gate: every path that puts an instruction on a run passes the same
doors (filter, budget, author). The repo is public, so only an OWNER / MEMBER /
COLLABORATOR comment is an instruction; anyone else's is logged and not routed.

A review comment ("fix this") on a PR the merge watch is tracking is an
instruction for THAT run — same branch, same PR (pushed force-with-lease). Landing
it as a fresh request would fork a second run off main and a second PR per review
(live: PR #1218 → #1219). The merge-conflict redispatch already does the right
thing — writes the instruction on the original run's payload and re-opens it —
and this is the same shape for a comment.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.data.rls import workspace_session_scope
from backend.workflow.application.run_status import move_run_status
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus

logger = structlog.get_logger(__name__)

#: Watches whose PR is still the run's live work. ``merged`` / ``abandoned`` are
#: over — a comment there is a new request, as for a PR nobody watched. ``failed``
#: stays: a red-CI PR is exactly the one a founder comments on.
_LIVE_WATCH = (
    MergeWatchStatus.PENDING_CI,
    MergeWatchStatus.MERGING,
    MergeWatchStatus.NEEDS_RESOLUTION,
    MergeWatchStatus.FAILED,
)

#: Runs that may take the instruction. ``shipped`` is final and ``cancelled`` is
#: the founder's own stop — neither is revived by a comment.
_APPEND_ONLY = (RunStatus.OPEN, RunStatus.RUNNING)
_REOPEN = (RunStatus.REVIEW_READY, RunStatus.FAILED)

#: ``comment.author_association`` values that may instruct a run. Anything else
#: (NONE, CONTRIBUTOR, FIRST_TIMER, ...) is a stranger on a public repo.
_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def pr_comment_of(payload: dict[str, Any]) -> tuple[str, int, str] | None:
    """``(repo, pr_number, text)`` when this github payload is a comment on a pull
    request, else ``None`` (an issue comment, or any other event)."""
    if payload.get("github_event") != "issue_comment":
        return None
    body = payload.get("body")
    if not isinstance(body, dict):
        return None
    issue = body.get("issue") or {}
    number = issue.get("number")
    text = str((body.get("comment") or {}).get("body") or "").strip()
    repo = payload.get("repo")
    # GitHub reports a PR as an issue carrying a ``pull_request`` key.
    if not (issue.get("pull_request") and isinstance(number, int) and text and repo):
        return None
    return str(repo), number, text


def _comment_author(payload: dict[str, Any]) -> tuple[str, str]:
    """``(login, author_association)`` of the comment, upper-cased association."""
    body = payload.get("body")
    comment = (body.get("comment") if isinstance(body, dict) else None) or {}
    login = str((comment.get("user") or {}).get("login") or "unknown")
    return login, str(comment.get("author_association") or "").upper()


async def route_pr_comment_to_run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    payload: dict[str, Any],
) -> uuid.UUID | None:
    """Hand a PR comment to the run that opened the PR; its id, or ``None``.

    ``None`` means "not ours to route" (no comment on a PR, an author who is not
    OWNER / MEMBER / COLLABORATOR, no live watch, a finished run) and the caller lands the delivery as a new request, as before.
    A run that is already ``open`` / ``running`` only gets the instruction queued
    — the next turn reads it; no second run is created. The caller commits.
    """
    parsed = pr_comment_of(payload)
    if parsed is None:
        return None
    repo, pr_number, text = parsed
    author, association = _comment_author(payload)
    if association not in _TRUSTED_ASSOCIATIONS:
        logger.info(
            "pr_comment_not_routed_untrusted_author",
            repo=repo,
            pr_number=pr_number,
            author=author,
            author_association=association or None,
        )
        return None

    async with workspace_session_scope(session, workspace_id):
        watch = (
            await session.execute(
                select(GithubMergeWatchRow).where(
                    GithubMergeWatchRow.workspace_id == workspace_id,
                    func.lower(GithubMergeWatchRow.repo) == repo.lower(),
                    GithubMergeWatchRow.pr_number == pr_number,
                    GithubMergeWatchRow.status.in_(_LIVE_WATCH),
                )
            )
        ).scalar_one_or_none()
        if watch is None:
            return None
        run = await session.get(ExecutionRun, watch.run_id)
        if run is None or run.status not in (*_APPEND_ONLY, *_REOPEN):
            return None

        run_payload = dict(run.payload or {})
        comments = list(run_payload.get("pr_comments") or [])
        comments.append({"pr_number": pr_number, "body": text, "author": author})
        run_payload["pr_comments"] = comments
        run.payload = run_payload
        if run.status in _REOPEN:
            await move_run_status(
                session, run, RunStatus.OPEN, reason=f"review comment on PR #{pr_number}"
            )
        await session.flush()

    logger.info(
        "pr_comment_routed_to_run",
        run_id=str(run.id),
        repo=repo,
        pr_number=pr_number,
        run_status=run.status.value,
    )
    return run.id


# ── the reading side: the drive loop turns the queued comments into turn context ──


def pr_comment_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """Review comments left on this run's PR, as turn-context messages.

    ``run.payload["pr_comments"]`` is a list of ``{pr_number, body, author}``
    intake appends (:func:`route_pr_comment_to_run`). Each becomes a user message telling the
    agent to fix the SAME branch/PR — never to open a new one."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    comments = payload.get("pr_comments")
    if not isinstance(comments, list):
        return []
    messages: list[dict[str, Any]] = []
    for entry in comments:
        if not isinstance(entry, dict) or not str(entry.get("body") or "").strip():
            continue
        messages.append(
            {
                "role": "user",
                "content": (
                    f"{entry.get('author') or 'A reviewer'} left a review comment on "
                    f"your pull request #{entry.get('pr_number')}:\n\n{entry['body']}\n\n"
                    "Address it on the SAME branch so the result is pushed to the "
                    "same pull request. Do not open a new pull request."
                ),
            }
        )
    return messages


def consume_pr_comments(run: ExecutionRun) -> None:
    """Clear the one-shot ``pr_comments`` key once injected (re-assigns the payload
    so SQLAlchemy sees the JSON change)."""
    payload = dict(run.payload or {})
    if "pr_comments" not in payload:
        return
    payload.pop("pr_comments", None)
    run.payload = payload


__all__ = [
    "consume_pr_comments",
    "pr_comment_messages",
    "pr_comment_of",
    "route_pr_comment_to_run",
]
