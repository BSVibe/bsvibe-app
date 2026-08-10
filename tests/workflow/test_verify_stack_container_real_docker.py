"""The derived container plan, run against a REAL docker daemon.

Every other test around this module compares strings. A stand-up command that
is shaped right and does not actually run is precisely the failure this whole
track exists to remove — the verification that passes because the test supplied
its own answer. So this one runs the derived commands and asserts on what the
container really contains.

Four properties, none of which a string comparison can reach:

1. the stand-up runs and the source ARRIVES inside the container;
2. commands wrapped by the plan execute IN THERE (not on the host);
3. the platform-poisoned dirs (``.venv`` / ``node_modules``) do NOT arrive — a
   macOS venv inside a Linux container is an environment that looks provisioned
   and is broken;
4. writing inside the container does NOT touch the founder's tree. Verification
   must not mutate the directory the agent works in.

Skipped without a docker daemon; CI runners have one. The ambient docker
context is used deliberately — this proves the command mechanics, while WHICH
daemon a real verification talks to is pinned by the caller
(``verification_stack._pinned``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.workflow.domain.verify_stack import derive_stack_plan

#: Tiny (~5MB) and irrelevant to the assertions: this test is about the stand-up
#: MECHANICS, and the toolchain→image derivation is covered as a unit. Pulling a
#: full python image per CI run would buy nothing here.
_IMAGE = "alpine:3.20"

_PROJECT = "bsvibe-verify-stack-selftest"
_TIMEOUT_S = 300


def _docker_missing() -> bool:
    if shutil.which("docker") is None:
        return True
    probe = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["docker", "info"],  # noqa: S607 — resolved above
        capture_output=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    return probe.returncode != 0


pytestmark = pytest.mark.skipif(_docker_missing(), reason="no docker daemon on this host")


def _run(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S602 — the command under test IS a shell string
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A repo-shaped tree: real source, plus the dirs that must not be copied."""
    (tmp_path / "hello.txt").write_text("from-the-founders-tree\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("#!mach-o-binary\n")
    (tmp_path / "apps" / "pwa" / "node_modules").mkdir(parents=True)
    (tmp_path / "apps" / "pwa" / "node_modules" / "left-pad.js").write_text("//\n")
    return tmp_path


def test_the_derived_container_really_stands_up_and_runs_the_checks(workspace: Path) -> None:
    plan = derive_stack_plan(
        repo_files=["pyproject.toml"],
        project=_PROJECT,
        workspace_path=str(workspace),
        metadata={"verify_stack": {"image": _IMAGE}},
    )
    assert plan is not None

    _run(plan.down)  # idempotent; clears a leftover from an interrupted run
    try:
        up = _run(plan.up)
        assert up.returncode == 0, f"stand-up failed: {up.stdout}\n{up.stderr}"

        got = _run(plan.wrap("cat hello.txt"))
        assert got.returncode == 0, got.stderr
        assert got.stdout.strip() == "from-the-founders-tree", "the source must arrive inside"

        inside = _run(plan.wrap("uname -s"))
        assert inside.stdout.strip() == "Linux", "the check must run in the container, not the host"

        poisoned = _run(plan.wrap("ls -d .venv apps/pwa/node_modules"))
        assert poisoned.returncode != 0, (
            "host-platform dirs were copied in — a venv of macOS binaries inside "
            f"a Linux container LOOKS provisioned and is broken: {poisoned.stdout}"
        )

        _run(plan.wrap("touch verification-litter.txt"))
        assert not (workspace / "verification-litter.txt").exists(), (
            "verification wrote into the founder's tree — the agent works there"
        )
    finally:
        down = _run(plan.down)
        assert down.returncode == 0, f"teardown failed: {down.stderr}"

    gone = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["docker", "inspect", _PROJECT],  # noqa: S607 — presence probed above
        capture_output=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert gone.returncode != 0, "teardown left the container behind"


def test_a_source_that_did_not_arrive_fails_the_stand_up(tmp_path: Path) -> None:
    """A copy that transfers NOTHING must not read as a successful stand-up.

    The copy is a pipeline and a pipeline reports its LAST stage, so the
    receiving ``tar -xf`` decides the verdict — and it exits 0 for an archive
    that is well-formed and empty. The stack would then be up with an empty
    /work, and the gate would read that as "this repo declares no toolchain":
    an infrastructure fault wearing the costume of an honest verdict, which is
    the disguise that cost a day in the 2026-08-09 E2E.

    (A source path that does not exist at all is caught more crudely — the
    receiving tar chokes on the truncated stream. This is the quieter case.)
    """
    (tmp_path / ".venv").mkdir()  # everything here is excluded → an empty archive
    plan = derive_stack_plan(
        repo_files=["pyproject.toml"],
        project=_PROJECT,
        workspace_path=str(tmp_path),
        metadata={"verify_stack": {"image": _IMAGE}},
    )
    assert plan is not None

    _run(plan.down)
    try:
        up = _run(plan.up)
        assert up.returncode != 0, "a source that never arrived must fail the stand-up loudly"
    finally:
        _run(plan.down)
