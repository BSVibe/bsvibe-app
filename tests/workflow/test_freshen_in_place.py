"""A stale client_attach PR is freshened WHERE ITS CHECKOUT IS.

The merge-watch worker keeps a PR mergeable by merging the base branch into the
run branch and pushing the result. It does that in a server-side clone — which a
client_attach product must never have (§3.5: the source lives only on the
founder's machine). So #738 stopped: the resolver returned ``None`` and such a
PR waited for a human forever.

The stop was right and the conclusion was too wide, in the exact shape #692
already corrected once for the gate: *where* the command runs was never the
question. ``git merge`` decides clean-vs-conflict by its exit code, and that
answer is the same on any machine holding the checkout. The founder's machine
holds it, the exec channel reaches it (#702), and the run's branch and worktree
are already named there (#734).

So the freshen becomes an injected capability rather than a block of git the
worker owns, and the two execution models supply their own — the same seam
shape as the sandbox (#692) and the check environment (#730).

What must NOT change:

* the state machine. clean → push + back to pending_ci; conflict → hand the run
  to the agent; failure → backoff, never a wedge.
* the privacy contract. No clone, no fetch of source, no token to that machine.
* a conflict is git's verdict, never a model's.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio


class _FounderBox:
    """The founder's machine. Records commands; scripts each one's exit code."""

    def __init__(self, *, fail_on: str | None = None, conflict: bool = False) -> None:
        self.commands: list[str] = []
        self._fail_on = fail_on
        self._conflict = conflict

    @property
    def workspace_mount(self) -> str:
        return "/founder/BStockReport/wt/19a99b51"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.commands.append(command)
        if self._fail_on and self._fail_on in command:
            return SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
        if self._conflict and command.startswith("git merge "):
            return SandboxResult(
                exit_code=1, stdout="CONFLICT (content): merge conflict", stderr="", timed_out=False
            )
        if command.startswith("git diff --name-only --diff-filter=U"):
            return SandboxResult(
                exit_code=0,
                stdout="src/report.py\ntests/test_report.py\n",
                stderr="",
                timed_out=False,
            )
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _freshen(box: _FounderBox, *, base: str = "main", branch: str = "run/19a99b51") -> Any:
    from backend.workflow.application.runtime.merge_watch_freshen import freshen_on_client_machine

    return freshen_on_client_machine(box=box, branch=branch, base_branch=base)


async def test_a_clean_freshen_merges_the_base_and_pushes_it() -> None:
    """The whole point. The base reaches the run branch and the freshened head
    goes back to github — done by git on the machine that has the checkout."""
    box = _FounderBox()

    outcome = await _freshen(box)

    assert outcome.status == "clean"
    joined = " | ".join(box.commands)
    assert "git fetch origin main" in joined
    assert "git merge origin/main" in joined
    assert "git push origin run/19a99b51" in joined


async def test_a_conflict_is_gits_verdict_and_names_the_files() -> None:
    """The conflicting paths are what the agent is handed to resolve. They come
    from git's own unmerged-file listing, never from parsing merge chatter."""
    box = _FounderBox(conflict=True)

    outcome = await _freshen(box)

    assert outcome.status == "conflict"
    assert outcome.conflict_paths == ("src/report.py", "tests/test_report.py")
    # …and nothing was pushed on a conflicted tree.
    assert not any(c.startswith("git push") for c in box.commands)


async def test_a_conflicted_merge_is_aborted_before_the_worktree_goes_back() -> None:
    """The worktree is a shared, reclaimable resource on someone else's machine
    (#736). Leaving it mid-merge would make the reaper refuse it forever, and
    the agent re-does the merge itself when it is re-dispatched anyway."""
    box = _FounderBox(conflict=True)

    await _freshen(box)

    assert any(c.startswith("git merge --abort") for c in box.commands)


async def test_a_failed_push_is_a_failure_not_a_clean_freshen() -> None:
    """Reporting ``clean`` on an unpushed merge tells the worker to wait for CI
    on a head github never received — the PR would sit pending_ci forever."""
    box = _FounderBox(fail_on="git push")

    outcome = await _freshen(box)

    assert outcome.status == "failed"


async def test_an_unreachable_machine_is_a_failure_never_a_conflict() -> None:
    """A machine that will not answer proves nothing about mergeability.
    Calling it a conflict would burn an agent run resolving nothing."""

    class _Dead(_FounderBox):
        async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> Any:
            raise OSError("worker gone")

    outcome = await _freshen(_Dead())

    assert outcome.status == "failed"


async def test_no_credential_and_no_source_travel_to_that_machine() -> None:
    """§3.5. The founder's own git credential does the push, exactly as #735's
    commit does — nothing here may carry a server-held token, a remote URL with
    one embedded, or a clone of anything."""
    box = _FounderBox()

    await _freshen(box)

    joined = " | ".join(box.commands)
    assert "clone" not in joined
    assert "https://" not in joined and "x-access-token" not in joined


async def test_the_worker_asks_its_freshener_and_keeps_its_state_machine() -> None:
    """The seam. The worker must no longer own the git — it owns the decisions
    that follow git's answer. Two backends, one state machine."""
    import inspect

    from backend.workflow.infrastructure.workers import merge_watch_worker

    source = inspect.getsource(merge_watch_worker)
    assert "BranchFreshener" in source
    # The server-side git moved out of the infrastructure worker with the seam.
    assert "_ensure_clone" not in source
    assert "merge_ref" not in source


async def test_the_picker_routes_each_execution_model_to_its_own_freshener() -> None:
    """The one decision that replaces the skip: which machine holds the
    checkout. Everything else about the freshen is identical."""
    from backend.workflow.application.runtime import merge_watch_freshen

    assert hasattr(merge_watch_freshen, "build_branch_freshener")
    source = __import__("inspect").getsource(merge_watch_freshen.build_branch_freshener)
    assert "product_runs_in_place" in source
    assert "client_machine_freshener" in source


async def test_an_incomplete_dispatch_context_is_a_failure_not_a_server_fallback() -> None:
    """[[bsvibe-no-implicit-routing]] applied to the worst case. If the founder's
    machine cannot be reached (no live worker, no dispatch context), the answer
    is "could not freshen" — NOT "freshen it here instead", which would clone
    their source onto the server to fix a mergeability problem."""
    from backend.workflow.application.runtime.merge_watch_freshen import (
        client_machine_freshener,
    )

    freshener = client_machine_freshener(
        # No redis / session factory / worker: nothing can reach that machine.
        box_factory=_no_box,
    )
    outcome = await freshener(uuid.uuid4(), branch="run/abc", base_branch="main")

    assert outcome.status == "failed"


async def _no_box(_run_id: uuid.UUID) -> Any:
    return None
