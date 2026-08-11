"""Opening the Pull Request — the half both execution models share.

A server-sandbox run is committed and pushed from its server-side checkout; a
``client_attach`` run committed and pushed itself on the founder's machine
(#734/#735). WHO produced the branch differs entirely. What happens once a
branch exists does not: open the PR, write its URL onto the Deliverable, enqueue
the CI-green auto-merge watch, comment back on the originating issue.

Kept in one place deliberately. When those steps lived inside the server-side
handler, the other model did not merely lack them — it had no delivery at all
(#723), and that is the shape of defect this package keeps producing: a feature
that exists for one execution model and silently not for the other.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.domain.delivery import ActionResult
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow

from ._context import _build_context
from ._merge_watch import enqueue_merge_watch
from ._resolver import GithubBinding

if TYPE_CHECKING:
    from ._github import GithubDeliveryDeps

logger = structlog.get_logger(__name__)


# github webhook event kinds that carry a numbered issue / PR the delivery can
# reply to (the framer's ``github_event`` tag on the TriggerEvent payload).
_GITHUB_ISSUE_EVENTS = frozenset({"issues", "pull_request", "issue_comment"})


async def _source_github_issue_number(
    session: AsyncSession, run_id: uuid.UUID | None
) -> int | None:
    """The github issue / PR number that triggered this run, so the delivery can
    close the loop back to it — a ``Closes #N`` ref in the PR body plus a comment
    carrying the PR link. Traces run → request → trigger_event (the run payload
    only keeps ``intent_text``; the issue number lives on the trigger envelope).

    ``None`` when the run was not github-issue sourced (a Direct chat run, a
    non-issue webhook, or any missing link) — so non-github runs are untouched.
    """
    if run_id is None:
        return None
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.request_id is None:
        return None
    request = await session.get(RequestRow, run.request_id)
    if request is None:
        return None
    trigger = await session.get(TriggerEventRow, request.trigger_event_id)
    if trigger is None:
        return None
    payload = trigger.payload or {}
    if payload.get("github_event") not in _GITHUB_ISSUE_EVENTS:
        return None
    body = payload.get("body") or {}
    target = body.get("issue") or body.get("pull_request") or {}
    number = target.get("number")
    return int(number) if isinstance(number, int) else None


async def _open_pr_and_settle(
    *,
    deps: GithubDeliveryDeps,
    binding: GithubBinding,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    run_id: uuid.UUID,
    branch: str,
    title: str,
    body: str,
    token: str,
    source_issue: int | None,
    action_prefix: str,
    pushed_by: str,
) -> list[ActionResult]:
    """Open the PR for ``branch`` and settle everything that follows from it.

    Shared by both execution models on purpose. WHO pushed the branch differs
    — the server for a sandbox run, the founder's own machine for a
    ``client_attach`` one (#735) — but from here on the work is identical and
    entirely API-side: open the PR, write its URL onto the Deliverable, enqueue
    the auto-merge watch, comment back on the originating issue. Letting the
    two models drift here is how one of them quietly loses a feature.
    """
    # 4. Open the PR via the github plugin's open_pr action. Routing (repo/base) is
    #    the founder-set config; head is the run branch; title/body from content.
    plugin = deps.plugins_by_name.get("github")
    if plugin is None:
        return [
            ActionResult(
                action=action_prefix,
                succeeded=False,
                error="github plugin not loaded",
            )
        ]
    ctx = _build_context(
        credentials={"token": token},
        config=dict(binding.account.delivery_config),
    )
    try:
        result = await deps.runner.dispatch_action(
            plugin,
            action_name="open_pr",
            context=ctx,
            kwargs={
                "repo": binding.repo,
                "head": branch,
                "base": binding.base_branch,
                "title": title,
                "body": body,
            },
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail like a plugin failure
        logger.warning(
            "github_delivery_open_pr_failed",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            error=str(exc),
        )
        return [ActionResult(action=action_prefix, succeeded=False, error=str(exc))]

    output = dict(result) if isinstance(result, dict) else {"result": result}
    # #362 — persist the PR URL onto the Deliverable (PWA/Brief link). Soft.
    await _persist_pr_url(deps.session_factory, deliverable_id, output.get("url"))

    # PR4 — enqueue the opened PR for CI-green auto-merge (gated; see _merge_watch).
    pr_number = output.get("pr_number")
    if isinstance(pr_number, int):
        await enqueue_merge_watch(
            deps,
            binding=binding,
            workspace_id=workspace_id,
            run_id=run_id,
            deliverable_id=deliverable_id,
            branch=branch,
            pr_number=pr_number,
        )

    # Close the loop back to the originating issue: comment with the PR link so
    # whoever filed it is notified (the PR body's ``Closes #N`` cross-links, but
    # a comment is the visible signal on the issue itself). Soft — the PR is
    # already open, so a comment hiccup must never fail the delivery.
    pr_url = output.get("url")
    if source_issue is not None and isinstance(pr_url, str) and pr_url:
        try:
            await deps.runner.dispatch_action(
                plugin,
                action_name="comment",
                context=ctx,
                kwargs={
                    "repo": binding.repo,
                    "issue_number": source_issue,
                    "body": f"🤖 Opened a pull request to resolve this: {pr_url}",
                },
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail like the PR-url writeback
            logger.warning(
                "github_delivery_issue_comment_failed",
                workspace_id=str(workspace_id),
                run_id=str(run_id),
                issue_number=source_issue,
                error=str(exc),
            )

    logger.info(
        "github_delivery_pr_opened",
        workspace_id=str(workspace_id),
        deliverable_id=str(deliverable_id),
        run_id=str(run_id),
        branch=branch,
        repo=binding.repo,
        pushed_by=pushed_by,
    )
    return [ActionResult(action=action_prefix, succeeded=True, output=output)]


async def _persist_pr_url(
    session_factory: async_sessionmaker[AsyncSession],
    deliverable_id: uuid.UUID,
    pr_url: object,
) -> None:
    """Write the opened PR's URL onto ``Deliverable.diff_url`` (#362).

    No-op when ``pr_url`` is falsy / not a string, or the deliverable is gone.
    Soft — never raises into the delivery path (a missed diff_url is cosmetic)."""
    if not isinstance(pr_url, str) or not pr_url:
        return
    from backend.workflow.infrastructure.db import Deliverable  # noqa: PLC0415 — lazy

    try:
        async with session_factory() as session:
            deliverable = await session.get(Deliverable, deliverable_id)
            if deliverable is not None:
                deliverable.diff_url = pr_url
                await session.commit()
    except Exception:  # noqa: BLE001 — diff_url write must never fail a delivered PR
        logger.warning(
            "github_delivery_diff_url_write_failed",
            deliverable_id=str(deliverable_id),
            exc_info=True,
        )


__all__ = ["_open_pr_and_settle", "_persist_pr_url", "_source_github_issue_number"]
