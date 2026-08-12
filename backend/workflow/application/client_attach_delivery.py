"""A client_attach run COMMITS its own work, on the founder's machine.

#723 turned git delivery off for this execution model, reasoning that "the
server never clones such a product, so there is no checkout here to commit and
push from". True about the server, and the wrong conclusion — the same one
in-place verify (#692) already overturned for the gate: *where* the checkout
lives was never the question. It lives on the founder's machine, the exec
channel already reaches it, and since #734 the run has a worktree of its own
there.

What the hole cost: every client_attach run left its work UNCOMMITTED in that
tree. Changes from different runs piled up unattributably; a run cancelled
mid-flight left files that a later session read as "that run produced nothing"
(it had produced everything); and nothing tied a change to the run that made it.

So the run commits before it settles. Committing is the RUN's own act — not a
consequence of delivering a deliverable, which a client_attach run may never
emit.

Fail-SOFT throughout, and recorded either way. A push that cannot happen (no
network, no credential, a protected branch) must not take down a run whose work
is finished and whose gate has already passed — but it must not pass silently
either, or the founder is back to not knowing where their work is.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

import structlog

from backend.workflow.domain.client_worktree import worktree_branch

logger = structlog.get_logger(__name__)

#: Local git plumbing — a commit, not a build.
_GIT_TIMEOUT_S = 60.0
#: A push crosses the network to github.
_PUSH_TIMEOUT_S = 300.0

#: Never committed even when the agent's tools happen to leave them behind.
#: Mirrors the server-side delivery's exclusions so a PR carries source, not
#: build litter — the two execution models must ship the same shape.
_UNCOMMITTABLE = (".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache")

#: How much of the intent rides in the commit subject.
_SUBJECT_CHARS = 60


@dataclass(frozen=True)
class GitDeliveryOutcome:
    """What actually happened to this run's work. Every field is observed."""

    branch: str
    committed: bool
    pushed: bool
    #: The first failure, recorded rather than raised. ``None`` when the ladder
    #: completed — including the honest "there was nothing to commit" case.
    error: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "committed": self.committed,
            "pushed": self.pushed,
            **({"error": self.error} if self.error else {}),
        }


def commit_subject(run: Any) -> str:
    """The commit's one-line subject: what was asked, and by which run.

    The run id is the load-bearing half. A commit nobody can attribute is what
    made a tree full of half-finished work unreadable in the first place.
    """
    payload = getattr(run, "payload", None) or {}
    intent = str(payload.get("intent_text") or payload.get("text") or "").strip()
    subject = " ".join(intent.split())[:_SUBJECT_CHARS] or "agent work"
    return f"work: {subject} (run-{str(run.id)[:8]})"


async def _run(box: Any, command: str, *, timeout_s: float) -> tuple[bool, str]:
    """Run one git command in the run's worktree; ``(ok, output)``."""
    try:
        # S604: the sandbox Protocol's own flag, not a subprocess call — this
        # box dispatches the string to the founder's machine as an exec task.
        res = await box.exec(command, timeout_s=timeout_s, shell=True)  # noqa: S604
    except Exception as exc:  # noqa: BLE001 — an unreachable machine is a soft failure
        return False, f"{type(exc).__name__}: {exc}"
    output = "\n".join(o for o in (res.stdout, res.stderr) if o)[-500:]
    if res.timed_out:
        return False, f"timed out after {timeout_s}s: {output}"
    return res.exit_code == 0, output


async def commit_and_push_run_work(*, box: Any, run: Any) -> GitDeliveryOutcome:
    """Commit this run's work on its own branch and push it.

    ``box`` is rooted in the run's worktree (#734), which is why ``git add -A``
    is the right scope here and would not have been before: that tree contains
    this run's changes and nothing else — not the founder's work-in-progress,
    not another run's.
    """
    branch = worktree_branch(run.id)
    litter = " ".join(shlex.quote(p) for p in _UNCOMMITTABLE)

    # Stage everything, THEN take the litter back out. The obvious one-liner —
    # `git add -A -- . :(exclude).venv …` — refuses outright the moment any of
    # those paths exists AND the repo ignores it: naming an ignored path in a
    # pathspec is what triggers "The following paths are ignored by one of your
    # .gitignore files", exit 1, nothing staged. Both conditions hold in any
    # real repo the moment the agent runs its own toolchain natively in the
    # worktree, which is what client_attach IS (live run `2abd398e`,
    # BStockReport). Measured on git 2.52; `--ignore-errors` does not help.
    #
    # `git add -A` with no pathspec never has that problem — .gitignore does its
    # work silently. The unstage then still earns its place: it catches litter
    # the repo does NOT ignore, which is the case the exclusions existed for.
    # `git reset` on paths that are absent, or were never staged, is a no-op
    # exit 0, and it leaves the files themselves alone.
    ok, out = await _run(box, "git add -A", timeout_s=_GIT_TIMEOUT_S)
    if ok:
        ok, out = await _run(box, f"git reset -q -- {litter}", timeout_s=_GIT_TIMEOUT_S)
    if not ok:
        logger.warning("client_attach_commit_stage_failed", run_id=str(run.id), error=out)
        return GitDeliveryOutcome(branch=branch, committed=False, pushed=False, error=out)

    # exit 0 = nothing staged. A run that changed nothing has nothing to ship,
    # and an empty commit would be a claim that it did.
    nothing, _ = await _run(box, "git diff --cached --quiet", timeout_s=_GIT_TIMEOUT_S)
    if nothing:
        logger.info("client_attach_nothing_to_commit", run_id=str(run.id))
        return GitDeliveryOutcome(branch=branch, committed=False, pushed=False)

    subject = shlex.quote(commit_subject(run))
    ok, out = await _run(box, f"git commit -m {subject}", timeout_s=_GIT_TIMEOUT_S)
    if not ok:
        logger.warning("client_attach_commit_failed", run_id=str(run.id), error=out)
        return GitDeliveryOutcome(branch=branch, committed=False, pushed=False, error=out)

    # The founder's OWN git credentials (their decision): the source is theirs,
    # the push is theirs, and no token travels from the server to their machine.
    ok, out = await _run(
        box, f"git push -u origin {shlex.quote(branch)}", timeout_s=_PUSH_TIMEOUT_S
    )
    if not ok:
        # The work is committed and safe on a named branch — losing the push is
        # recoverable, so it is recorded and the run still settles.
        logger.warning("client_attach_push_failed", run_id=str(run.id), error=out)
        return GitDeliveryOutcome(branch=branch, committed=True, pushed=False, error=out)

    logger.info("client_attach_work_pushed", run_id=str(run.id), branch=branch)
    return GitDeliveryOutcome(branch=branch, committed=True, pushed=True)


async def land_client_attach_deliverable(
    session: Any,
    *,
    run: Any,
    attempt_id: Any,
    changed_paths: list[str],
    final_text: str,
    gate: dict[str, Any] | None,
    redis_client: Any,
    settings: Any,
) -> Any | None:
    """Make this run's finished work REACH the founder — or honestly, nothing.

    #692 withheld the verified-terminal artifacts from this execution model on
    the premise that "the server holds no source, so there is nothing to
    deliver". Two lifts overturned it: since #735 the work is pushed to a branch
    the server can point at, and #738 opens the PR from it. What was left was a
    run that finished, committed, pushed — and reached nobody: no deliverable,
    no Safe Mode item, no telegram, nothing in the Brief. The founder found out
    by running ``git log``.

    ``changed_paths`` comes from the founder's own git (``_changed_paths``),
    never from server-side ``written_paths`` — which is always empty here
    because the agent used the CLI's native tools.

    **Empty means silence.** A deliverable is a claim that work happened, the
    same claim #735 refuses to make with an empty commit. A run whose tree git
    reports unchanged gets no approval item: manufacturing one trains the
    founder to approve without looking.

    Returns the Deliverable, or ``None`` when there was nothing to show.
    """
    if not changed_paths:
        logger.info("client_attach_no_deliverable_nothing_changed", run_id=str(run.id))
        return None

    from backend.workflow.application.run_persistence import (  # noqa: PLC0415 — cycle break
        land_verified_artifacts,
    )

    deliverable = await land_verified_artifacts(
        session,
        run=run,
        attempt_id=attempt_id,
        written_paths=changed_paths,
        final_text=final_text,
        # The in-place gate's own record, in the shape the summary composer
        # reads. ``None`` when the repo declared no toolchain — legitimately
        # gateless, so the summary says nothing about a proof rather than
        # implying one.
        verdict_result={"derived_gate": gate} if gate is not None else None,
        redis_client=redis_client,
        settings=settings,
    )
    logger.info(
        "client_attach_deliverable_landed",
        run_id=str(run.id),
        deliverable_id=str(deliverable.id),
        changed=len(changed_paths),
    )
    return deliverable


__all__ = [
    "GitDeliveryOutcome",
    "commit_and_push_run_work",
    "commit_subject",
    "land_client_attach_deliverable",
]
