"""A client_attach run works in its OWN worktree, not in the founder's checkout.

Until now such a run edited the founder's working directory directly. Three
things follow from that, and all three were observed:

* **The tree accumulates uncommitted work.** Files from a run cancelled hours
  earlier were still sitting there, and a later session read the clean-looking
  history as "that run produced nothing" — it had produced everything.
* **Two runs cannot proceed at once.** They edit the same files with no
  boundary between them.
* **The founder's own work-in-progress is in the blast radius.** A commit step
  cannot tell their changes from the run's.

A git worktree fixes all three at once and is already this founder's idiom
(``bsvibe-app/wt/<branch>``): the run gets a real checkout of its own branch, on
the same disk, sharing one object store.
"""

from __future__ import annotations

import uuid

from backend.workflow.domain.client_worktree import (
    client_run_worktree,
    orphan_reclaim_command,
    parse_worktree_shorts,
    worktree_branch,
    worktree_list_command,
    worktree_provision_command,
    worktree_reclaim_command,
)

_REPO = "/Users/founder/Works/BStockReport-client"
_RUN = uuid.UUID("a2c2894a-f0be-491c-a585-7b69eaa972b0")


class TestPath:
    def test_the_path_is_derived_from_the_run_not_invented(self) -> None:
        """Two callers derive it independently — the agent's dispatch and the
        verification box — and they MUST land on the same directory. A path
        passed from one to the other would be a wiring dependency; a derivation
        cannot drift."""
        assert client_run_worktree(_REPO, _RUN) == client_run_worktree(_REPO, _RUN)
        assert client_run_worktree(_REPO, _RUN).startswith(f"{_REPO}/")

    def test_the_path_names_the_run(self) -> None:
        """A directory nobody can attribute is debris. The run id in the name is
        what lets a founder — or a reaper — say what this is and whether it is
        still wanted."""
        assert "a2c2894a" in client_run_worktree(_REPO, _RUN)

    def test_distinct_runs_get_distinct_worktrees(self) -> None:
        other = uuid.UUID("b09f0920-05d1-41aa-987b-7b745aa4e4d4")
        assert client_run_worktree(_REPO, _RUN) != client_run_worktree(_REPO, other)

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        assert client_run_worktree(f"{_REPO}/", _RUN) == client_run_worktree(_REPO, _RUN)

    def test_the_branch_names_the_run_too(self) -> None:
        assert worktree_branch(_RUN) == "run/a2c2894a"


class TestProvisionCommand:
    def test_it_creates_the_worktree_on_its_own_branch(self) -> None:
        cmd = worktree_provision_command(_REPO, _RUN)
        assert "git -C" in cmd and _REPO in cmd
        assert "worktree add" in cmd
        assert "run/a2c2894a" in cmd

    def test_it_is_idempotent_because_runs_resume(self) -> None:
        """A resumed or retried run re-enters this step. ``worktree add`` on an
        existing path is a hard error, and a run that dies there never reaches
        the work it was retrying — the reentrancy trap this codebase has already
        paid for once with a stalled clone."""
        cmd = worktree_provision_command(_REPO, _RUN)
        assert "[ -d" in cmd or "test -d" in cmd, (
            f"provisioning must check for an existing worktree first: {cmd!r}"
        )

    def test_it_keeps_the_founders_checkout_clean(self) -> None:
        """The worktree lives inside the repo, so without this it shows up as an
        untracked directory in the founder's ``git status`` forever — which is
        the very mess this change exists to end. ``.git/info/exclude`` is the
        right place: it is LOCAL, so BSVibe never edits a tracked file to make
        room for itself."""
        cmd = worktree_provision_command(_REPO, _RUN)
        assert ".git/info/exclude" in cmd

    def test_the_founders_own_checkout_is_never_switched(self) -> None:
        """The point of a worktree is that their branch and their
        work-in-progress are untouched. A checkout/switch here would defeat it."""
        cmd = worktree_provision_command(_REPO, _RUN)
        for forbidden in ("git checkout", "git switch", "git stash", "git reset"):
            assert forbidden not in cmd, f"must not disturb the founder's checkout: {forbidden}"


class TestReclaimCommand:
    """#734 makes a worktree per run and nothing gives one back.

    Every client_attach run leaves a checkout of the whole repo on the founder's
    machine, forever. This machine's disk filling up is not a degradation, it is
    an unrecoverable brick — and the previous leak of exactly this shape
    (#665/#666) ran for months because ``git worktree remove`` had quietly
    no-opped and everyone read exit 0 as "reclaimed".
    """

    def test_it_never_forces(self) -> None:
        """The one line that must never change.

        Measured against real git (2.52): without ``--force``, ``remove``
        REFUSES a tree holding modified or untracked files — the tree whose
        contents exist nowhere else. That refusal is the entire safety
        mechanism, so the reaper does not reimplement the check, it inherits it.
        With ``--force`` this function deletes the founder's only copy of work a
        cancelled run produced, which is the exact loss #734/#735 exist to end.
        """
        cmd = worktree_reclaim_command(_REPO, _RUN)
        assert "--force" not in cmd and " -f " not in cmd, (
            f"reclaim must never force — git's refusal IS the safety net: {cmd!r}"
        )

    def test_it_reclaims_this_runs_worktree(self) -> None:
        cmd = worktree_reclaim_command(_REPO, _RUN)
        assert "worktree remove" in cmd
        assert client_run_worktree(_REPO, _RUN) in cmd

    def test_it_proves_the_directory_is_gone(self) -> None:
        """#665/#666: ``git worktree remove`` returned 0 while removing nothing,
        so the leak reported success for months. Exit 0 here has to MEAN the
        directory is gone, not that a command declined to complain."""
        cmd = worktree_reclaim_command(_REPO, _RUN)
        assert "! -d" in cmd, f"success must be observed, not assumed: {cmd!r}"

    def test_it_keeps_the_branch(self) -> None:
        """Removal touches neither the branch nor the objects, so the run's
        commits stay reachable even when the push failed. Deleting the branch
        would turn a reaper into the data loss it exists to prevent."""
        cmd = worktree_reclaim_command(_REPO, _RUN)
        for forbidden in ("branch -D", "branch -d", "push --delete", "git reset"):
            assert forbidden not in cmd, f"the run's commits must survive: {forbidden}"

    def test_it_does_not_touch_the_founders_checkout(self) -> None:
        cmd = worktree_reclaim_command(_REPO, _RUN)
        for forbidden in ("git checkout", "git switch", "git stash", "rm -rf"):
            assert forbidden not in cmd, f"must not disturb the founder's checkout: {forbidden}"


class TestOrphanSweep:
    """#736 reclaims at ``release``. A run whose process was KILLED never gets
    there, and its checkout stays on the founder's disk with nobody to name it.

    The founder's machine cannot tell those apart on its own: a run that has
    only read so far has a clean, young worktree that looks exactly like an
    abandoned one. Which runs are still going is the SERVER's knowledge, so the
    machine lists what is there and the server decides what may go.
    """

    def test_it_lists_worktrees_from_the_repo(self) -> None:
        cmd = worktree_list_command(_REPO)
        assert f"git -C {_REPO}" in cmd
        assert "worktree list" in cmd
        assert "--porcelain" in cmd, "the human format is not a parsing contract"

    def test_it_reads_only_our_own_worktrees(self) -> None:
        """The founder's repo has worktrees of their own — this is their idiom,
        which is exactly why #734 adopted it. Sweeping one of those would delete
        the branch they are working in."""
        listing = (
            f"worktree {_REPO}\nHEAD abc\nbranch refs/heads/main\n\n"
            f"worktree {_REPO}/wt/a2c2894a\nHEAD def\nbranch refs/heads/run/a2c2894a\n\n"
            f"worktree {_REPO}/../their-own-checkout\nHEAD 999\n\n"
            f"worktree {_REPO}/wt/b09f0920\nHEAD 111\n\n"
        )
        assert parse_worktree_shorts(listing, _REPO) == ["a2c2894a", "b09f0920"]

    def test_an_empty_listing_sweeps_nothing(self) -> None:
        assert parse_worktree_shorts("", _REPO) == []

    def test_reclaiming_an_orphan_is_the_same_careful_removal(self) -> None:
        """No second, laxer path. The sweep touches trees nobody is watching, so
        if anything it needs git's refusal MORE than the run's own reclaim does.
        """
        cmd = orphan_reclaim_command(_REPO, f"{_REPO}/wt/a2c2894a")
        assert "--force" not in cmd
        assert "! -d" in cmd
        assert "worktree remove" in cmd
