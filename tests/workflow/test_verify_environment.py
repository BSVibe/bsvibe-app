"""Where a verification check actually RUNS — the seam, not the stand-up.

#730 derived what environment a product gets; nothing used it. This is the
wiring: the run's checks execute INSIDE that environment, or the run says why
they could not. A stack stood up next to checks that keep running on the
founder's machine isolates nothing while claiming to.

Fail-CLOSED throughout, because the distinctions are what make the claim mean
anything:

* an environment exists → commands go through it, and the evidence records WHICH
  environment ran them;
* the product declared host execution (some checks legitimately need host
  resources) → unchanged behaviour, explicitly;
* no slot free / the boot failed → **no box at all**. Falling back to the host
  would silently produce a result of a different kind than the one claimed.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import SandboxError, SandboxResult

pytestmark = pytest.mark.asyncio

_CLI_REPO = ["pyproject.toml", "uv.lock"]


class _Box:
    """The founder's machine: records every command it is asked to run."""

    runs_in_place = True
    provisions_venv = False

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.commands: list[str] = []
        self._fail_on = fail_on

    @property
    def workspace_mount(self) -> str:
        return "/founder/BStockReport"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.commands.append(command)
        if self._fail_on and self._fail_on in command:
            return SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        if rel_path != "uv.lock":
            raise SandboxError(f"missing {rel_path}")
        return b"lock"

    async def write_file(self, rel_path: str, content: bytes) -> None:
        raise AssertionError("the server never writes to the founder's tree")

    async def list_dir(self, rel_path: str) -> list[str]:
        if rel_path != ".":
            # No ``deploy/`` in a CLI repo. An unreadable/absent directory
            # contributes nothing rather than a guess.
            raise SandboxError(f"no such directory {rel_path}")
        return ["pyproject.toml", "uv.lock", "src/"]


def _open(box: Any, **over: Any) -> Any:
    from backend.workflow.application.verify_environment import open_check_environment

    kwargs: dict[str, Any] = {
        "box": box,
        "slot_project": "verify-slot-0",
        "repo_files": _CLI_REPO,
        "metadata": None,
        "docker_context": "colima",
        "boot_timeout_s": 1800.0,
    }
    kwargs.update(over)
    return open_check_environment(**kwargs)


async def test_a_check_runs_inside_the_environment() -> None:
    """The whole lift. The command the caller asks for must reach the container,
    not the founder's shell."""
    box = _Box()
    async with _open(box) as env:
        assert env.box is not None
        await env.box.exec("uv run pytest -q", timeout_s=60.0, shell=True)

    ran = box.commands[-2]  # last is the teardown
    assert "docker exec" in ran
    assert "verify-slot-0" in ran
    assert "uv run pytest -q" in ran


async def test_absolute_paths_move_into_the_environment() -> None:
    """``_run_command_checks`` builds ``{workspace_mount}/.venv/bin`` to put the
    project's tools on PATH. A founder-machine path means nothing inside the
    container and resolves to nothing — silently, so every command would run
    without its toolchain and fail for the wrong reason."""
    box = _Box()
    async with _open(box) as env:
        assert env.box is not None
        assert env.box.workspace_mount == "/work"


async def test_the_environment_provisions_its_own_dependencies() -> None:
    """A client_attach box declines venv provisioning on purpose — ``uv sync``
    in the founder's own tree is an unasked-for mutation. A disposable container
    is the opposite case: nothing is installed in it yet, and reproducing the
    declared dependencies there is the entire point."""
    box = _Box()
    async with _open(box) as env:
        assert env.box is not None
        assert env.box.provisions_venv is True


async def test_reads_come_from_the_founders_tree() -> None:
    """Manifests and git answers are about the source under test. Only the
    COMMANDS need the isolated environment."""
    box = _Box()
    async with _open(box) as env:
        assert env.box is not None
        assert await env.box.read_file("uv.lock", 64) == b"lock"


async def test_a_product_that_declared_host_execution_is_unchanged() -> None:
    """Some checks legitimately need host resources (BStockReport's
    ``BLOASIS_DB_PATH`` is a SQLite file on the founder's disk). Declaring that
    must keep working exactly as before — including the box's own venv policy."""
    box = _Box()
    async with _open(box, metadata={"verify_stack": None}) as env:
        assert env.box is box
        assert env.kind == "host"
        assert env.unavailable is None


async def test_no_slot_free_yields_no_box_rather_than_the_host() -> None:
    """Fail-closed. Running the checks on the host instead would answer a
    DIFFERENT question than the one the report will claim was answered — and
    nobody reading it could tell."""
    box = _Box()
    async with _open(box, slot_project=None) as env:
        assert env.box is None
        assert env.unavailable is not None
        assert "slot" in env.unavailable.lower()

    assert box.commands == [], "nothing may be dispatched without an environment"


async def test_a_failed_boot_yields_no_box_and_says_why() -> None:
    box = _Box(fail_on="docker run")
    async with _open(box) as env:
        assert env.box is None
        assert env.unavailable is not None
        assert "boom" in env.unavailable


async def test_the_environment_is_recorded_for_the_evidence_trail() -> None:
    """ "Which environment did this check run in" is half of what the check
    means — §3.1 of the design: the environment is a FIELD of the check, not a
    separate kind of proof state."""
    box = _Box()
    async with _open(box) as env:
        assert env.describe() == {
            "kind": "container",
            "source": "container",
            "image": "ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
            "project": "verify-slot-0",
        }


async def test_the_environment_goes_away_even_when_the_body_raises() -> None:
    box = _Box()
    with pytest.raises(RuntimeError, match="check blew up"):
        async with _open(box):
            raise RuntimeError("check blew up")

    assert "docker rm -f" in box.commands[-1]


# ── the wiring: the run's settle path must actually OPEN one ─────────────────


async def test_the_client_attach_terminal_runs_its_gate_inside_an_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate call site is where this subsystem is switched on. Derived,
    stood up, and never reached is the failure mode this codebase keeps
    finding — a signal recorded and acted on by nobody."""
    from contextlib import asynccontextmanager

    from backend.workflow.application import _loop_context, inplace_gate, verify_environment

    sentinel = CheckEnvironmentStub()
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_open(**kwargs: Any) -> Any:
        seen["opened"] = kwargs
        yield sentinel

    async def _fake_gate(_service: Any, **kwargs: Any) -> dict[str, Any]:
        seen["environment"] = kwargs.get("environment")
        return {"commands": [], "passed": True, "proved": True}

    monkeypatch.setattr(verify_environment, "open_run_check_environment", _fake_open)
    monkeypatch.setattr(inplace_gate, "run_inplace_gate", _fake_gate)

    box = _Box()
    orch = _Orch()
    result = await _loop_context.settle_client_attach(
        orch,
        run=_Run(),
        work_step=_Step(),
        attempt=_Attempt(),
        box=box,
        messages=[],
        baseline=None,
        cycle=0,
    )

    assert result is not None, "the run still settles"
    assert seen["environment"] is sentinel, "the gate must receive the environment"
    assert seen["opened"]["box"] is box


class CheckEnvironmentStub:
    box = None
    kind = "host"
    unavailable = None

    def describe(self) -> dict[str, Any]:
        return {"kind": "host"}


class _Orch:
    """The minimum of the orchestrator that settling touches."""

    _max_cycles = 3

    def __init__(self) -> None:
        self._session = _NullSession()
        # The settle path lands the run's deliverable through the same helper
        # the sandbox terminal uses, so it needs the same two collaborators.
        self._redis_client = None
        from backend.config import get_settings

        self._settings = get_settings()

    def _verifier(self) -> Any:
        return None

    async def _audit(self, *args: Any, **kwargs: Any) -> None:
        return None


class _NullSession:
    async def flush(self) -> None:
        return None


class _Run:
    def __init__(self) -> None:
        import uuid

        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {}


class _Step:
    def __init__(self) -> None:
        import uuid

        self.id = uuid.uuid4()
        self.status = None
        self.proof_state = None


class _Attempt:
    def __init__(self) -> None:
        import uuid

        self.id = uuid.uuid4()
        self.phase = None
        self.finished_at = None


# ── against a real database: the slot lock has to live somewhere it survives ──


_needs_pg = pytest.mark.skipif(
    not __import__("tests._support", fromlist=["use_real_pg"]).use_real_pg(),
    reason="the slot lease is a PostgreSQL advisory lock",
)


@_needs_pg
async def test_the_run_level_entry_takes_a_real_slot_and_stands_the_environment_up() -> None:
    """The composition, against the database it will meet in production.

    The slot lock is session-scoped, so WHERE it is held is the whole design:
    not on the run's own session (which commits — and so returns its connection
    to the pool — repeatedly through verification, by design), but on a
    connection whose death is what frees the slot.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.workflow.application.verify_environment import open_run_check_environment
    from backend.workflow.infrastructure.db import ExecutionBase
    from tests._support import db_engine

    box = _Box()
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            async with open_run_check_environment(session=session, run=_Run(), box=box) as env:
                assert env.kind == "container", env.describe()
                assert env.describe()["project"] == "verify-slot-0"
                assert env.box is not None
                # The run's OWN session still works while the slot is held —
                # the lock is not sitting on its connection.
                assert await session.scalar(__import__("sqlalchemy").text("SELECT 1")) == 1

    assert any("docker run" in c for c in box.commands), box.commands
    assert "docker rm -f" in box.commands[-1], "the environment must be torn down"


async def test_the_settled_run_commits_its_work_before_it_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring. Committing is the RUN's own act — a client_attach run may
    never emit a deliverable, so hanging it off delivery would leave exactly the
    runs whose work most needs attributing with nothing committed."""
    from contextlib import asynccontextmanager

    from backend.workflow.application import (
        _loop_context,
        client_attach_delivery,
        inplace_gate,
        verify_environment,
    )

    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def _fake_open(**kwargs: Any) -> Any:
        yield CheckEnvironmentStub()

    async def _fake_gate(_service: Any, **kwargs: Any) -> dict[str, Any]:
        seen["gate_ran"] = True
        return {"commands": [], "passed": True, "proved": True}

    async def _fake_commit(*, box: Any, run: Any, baseline: str | None = None) -> Any:
        seen["committed_after_gate"] = seen.get("gate_ran", False)
        return client_attach_delivery.GitDeliveryOutcome(
            branch="run/x", committed=True, pushed=True
        )

    monkeypatch.setattr(verify_environment, "open_run_check_environment", _fake_open)
    monkeypatch.setattr(inplace_gate, "run_inplace_gate", _fake_gate)
    monkeypatch.setattr(client_attach_delivery, "commit_and_push_run_work", _fake_commit)

    result = await _loop_context.settle_client_attach(
        _Orch(),
        run=_Run(),
        work_step=_Step(),
        attempt=_Attempt(),
        box=_Box(),
        messages=[],
        baseline=None,
        cycle=0,
    )

    assert result is not None
    assert seen.get("committed_after_gate") is True, (
        "the commit must come AFTER the gate — the agent's fixes for a gate "
        "failure are part of the work being committed"
    )
