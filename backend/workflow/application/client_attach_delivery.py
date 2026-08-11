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
    excludes = " ".join(shlex.quote(f":(exclude){p}") for p in _UNCOMMITTABLE)

    ok, out = await _run(box, f"git add -A -- . {excludes}", timeout_s=_GIT_TIMEOUT_S)
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


__all__ = ["GitDeliveryOutcome", "commit_and_push_run_work", "commit_subject"]
