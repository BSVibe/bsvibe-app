"""What a verification command leaves in the checkout is never committed with the run's work (#1267).

Prod #1263 (run 14e222a1, 2026-10-29): the product's final ``pnpm install`` put pnpm's store at
the sandbox mount root — ``<checkout>/.pnpm-store/`` — and the next round's ``commit_worktree``
(``git add -A``) committed 18,745 of its files onto the run branch. The cause was the sandbox's
mount, not the stack: no ignore list keyed on tool names foresees it.

So the files that appear in the checkout DURING verification go into that checkout's own
``.git/info/exclude`` — not deleted, and never written into the repository's ``.gitignore``.

Every case runs real git in a temporary repository through a real shell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from backend.workflow.application.verification_leftovers import leftovers_excluded
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("node_modules/\n")
    (root / "src.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _staged_after_add_all(root: Path) -> set[str]:
    _git(root, "add", "-A")
    return set(_git(root, "diff", "--cached", "--name-only").split())


def _tool_cache(root: Path) -> None:
    """What a verification command does: writes a cache tree into the checkout."""
    for i in range(3):
        d = root / "tool-cache" / "v1" / f"{i:02d}"
        d.mkdir(parents=True)
        (d / "blob").write_text("cached\n")
    (root / "probe.log").write_text("probe output\n")


async def test_files_a_verification_creates_are_not_committed_and_not_deleted(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    async with leftovers_excluded(NoopSandboxSession(str(root))):
        _tool_cache(root)

    assert _staged_after_add_all(root) == set()
    assert (root / "tool-cache" / "v1" / "00" / "blob").exists()
    assert (root / "probe.log").exists()


async def test_the_agents_own_new_files_are_still_committed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "feature.py").write_text("y = 2\n")  # written by the agent BEFORE verification
    async with leftovers_excluded(NoopSandboxSession(str(root))):
        _tool_cache(root)

    assert _staged_after_add_all(root) == {"feature.py"}


async def test_a_path_the_agent_writes_later_is_committed_again(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    box = NoopSandboxSession(str(root))
    async with leftovers_excluded(box):
        _tool_cache(root)
    # Next round: the agent writes a real file where verification once left one.
    (root / "probe.log").write_text("the agent's own log format\n")
    async with leftovers_excluded(box, keep=["probe.log"]):
        pass

    assert _staged_after_add_all(root) == {"probe.log"}


async def test_the_repositorys_gitignore_is_never_touched(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = (root / ".gitignore").read_text()
    async with leftovers_excluded(NoopSandboxSession(str(root))):
        _tool_cache(root)

    assert (root / ".gitignore").read_text() == before
    assert _git(root, "status", "--porcelain", "--", ".gitignore") == ""


async def test_files_inside_ignored_paths_are_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    async with leftovers_excluded(NoopSandboxSession(str(root))):
        (root / "node_modules").mkdir()
        (root / "node_modules" / "pkg.js").write_text("x\n")

    exclude = (root / ".git" / "info" / "exclude").read_text()
    assert "node_modules" not in exclude


async def test_the_founders_machine_is_never_touched(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    class _InPlace(NoopSandboxSession):
        runs_in_place = True

    async with leftovers_excluded(_InPlace(str(root))):
        _tool_cache(root)

    exclude = root / ".git" / "info" / "exclude"
    assert "tool-cache" not in (exclude.read_text() if exclude.exists() else "")


async def test_a_tree_that_is_not_a_repository_is_a_quiet_no_op(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    async with leftovers_excluded(NoopSandboxSession(str(plain))):
        (plain / "x").write_text("x\n")
    assert (plain / "x").exists()


async def test_a_body_that_raises_still_excludes_and_reraises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    try:
        async with leftovers_excluded(NoopSandboxSession(str(root))):
            _tool_cache(root)
            raise RuntimeError("verification blew up")
    except RuntimeError:
        pass
    assert _staged_after_add_all(root) == set()


# ── wired where verification runs ────────────────────────────────────────────


async def test_the_orchestrators_verify_excludes_what_verification_left(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.infrastructure.sandbox import NoopSandboxManager
    from tests._support import memory_session

    root = _repo(tmp_path)

    class _Verifier:
        async def verify(self, **_kwargs: Any) -> str:
            _tool_cache(root)
            return "verdict"

    async with memory_session() as session:
        orch = RunOrchestrator(session=session, llm=object(), sandbox_manager=NoopSandboxManager())
        monkeypatch.setattr(orch, "_verifier", lambda: _Verifier())
        verdict = await orch._verify(
            run=object(),
            work_step=object(),
            attempt=object(),
            contract=object(),
            box=NoopSandboxSession(str(root)),
            written_paths=[],
            final_text="",
        )

    assert verdict == "verdict"
    assert _staged_after_add_all(root) == set()


async def test_the_quick_checks_exclude_what_they_left(tmp_path: Path, monkeypatch: Any) -> None:
    from backend.workflow.application import precheck
    from backend.workflow.domain.declared_checks import DeclaredChecks

    root = _repo(tmp_path)

    async def _declared(*_a: Any) -> DeclaredChecks:
        return DeclaredChecks(precheck=("mkdir -p tool-cache/x && touch tool-cache/x/blob",))

    monkeypatch.setattr(precheck, "product_declared_checks", _declared)
    run = type("_Run", (), {"product_id": None})()
    await precheck.precheck_messages(object(), run, NoopSandboxSession(str(root)), "done")  # type: ignore[arg-type]

    assert (root / "tool-cache" / "x" / "blob").exists()
    assert _staged_after_add_all(root) == set()
