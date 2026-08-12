"""A client_attach run's finished work has to REACH the founder.

The server-sandbox terminal lands a Deliverable + DeliveryEventRow + settle
activity — that is what puts an item in the Safe Mode queue, what sends the
telegram, what opens the PR (#738), and what the Brief reads. Its own docstring
says the contract is "the SAME regardless of compute backend".

client_attach did not go through it. A run finished, committed, pushed (#735),
and then nothing existed on the server pointing at any of it: no deliverable, no
approval, no notification. The founder learned their work had happened by
running ``git log`` themselves — which is the same "half-wired subsystem" shape
this repo keeps finding, with the visible half (the run says ``verified``) built
and the half that reaches a person missing.

Two things the landing must NOT do, both of which would be worse than the hole:

* it must not lift ``proof_state``. Only a gate that RAN and passed proves
  anything (the honesty ratchet); landing a deliverable is about visibility.
* it must not land for a run that changed nothing. A deliverable is a claim that
  work happened — the same rule #735 holds for the empty commit.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxResult

from .._support import memory_session

pytestmark = pytest.mark.asyncio


class _Box:
    """The founder's machine. Answers the git probes the settle path makes."""

    runs_in_place = True
    provisions_venv = False

    def __init__(self, *, changed: tuple[str, ...] = ("src/report.py",)) -> None:
        self.commands: list[str] = []
        self._changed = changed

    @property
    def workspace_mount(self) -> str:
        return "/founder/BStockReport"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.commands.append(command)
        if command.startswith("git status --porcelain"):
            body = "\n".join(f" M {p}" for p in self._changed)
            return SandboxResult(exit_code=0, stdout=body, stderr="", timed_out=False)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        raise SandboxError(f"missing {rel_path}")

    async def write_file(self, rel_path: str, content: bytes) -> None:
        raise AssertionError("the server never writes to the founder's tree")

    async def list_dir(self, rel_path: str) -> list[str]:
        return ["pyproject.toml"]


class _Orch:
    """The minimum of the orchestrator the settle path touches."""

    _max_cycles = 3

    def __init__(self, session: Any) -> None:
        self._session = session
        self._redis_client = None
        from backend.config import get_settings

        self._settings = get_settings()

    def _verifier(self) -> Any:
        return None

    async def _audit(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Step:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = None
        self.proof_state = ProofState.UNTESTED


class _Attempt:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.phase = None
        self.finished_at = None


async def _seed_run(session: Any, *, intent: str = "주간 리포트를 고쳐줘") -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    session.add(run)
    await session.flush()
    return run


def _stub_settle_dependencies(
    monkeypatch: pytest.MonkeyPatch, *, gate: dict[str, Any] | None
) -> None:
    """Everything between the agent and the landing, held still.

    The gate and the commit have their own live-proven implementations; what is
    under test here is whether the run's finished work becomes something the
    founder can see.
    """
    from contextlib import asynccontextmanager

    from backend.workflow.application import (
        client_attach_delivery,
        inplace_gate,
        verify_environment,
    )

    @asynccontextmanager
    async def _fake_open(**kwargs: Any) -> Any:
        yield _EnvironmentStub()

    async def _fake_gate(_service: Any, **kwargs: Any) -> dict[str, Any] | None:
        return gate

    async def _fake_commit(*, box: Any, run: Any, baseline: str | None = None) -> Any:
        return client_attach_delivery.GitDeliveryOutcome(
            branch=f"run/{str(run.id)[:8]}", committed=True, pushed=True
        )

    monkeypatch.setattr(verify_environment, "open_run_check_environment", _fake_open)
    monkeypatch.setattr(inplace_gate, "run_inplace_gate", _fake_gate)
    monkeypatch.setattr(client_attach_delivery, "commit_and_push_run_work", _fake_commit)


class _EnvironmentStub:
    box = None
    kind = "host"
    unavailable = None

    def describe(self) -> dict[str, Any]:
        return {"kind": "host"}


_PROVED_GATE: dict[str, Any] = {
    "origin": "derived_in_place",
    "applicable": True,
    "commands": [
        {"command": "uv run pytest -q", "kind": "test", "status": "passed"},
        {"command": "uv run ruff check .", "kind": "lint", "status": "passed"},
    ],
    "passed": True,
    "proved": True,
}


async def _settle(
    session: Any, *, box: _Box, gate: dict[str, Any] | None, final_text: str = "다 고쳤어요."
) -> tuple[Any, ExecutionRun, _Step]:
    from backend.workflow.application import _loop_context

    run = await _seed_run(session)
    step = _Step()
    result = await _loop_context.settle_client_attach(
        _Orch(session),
        run=run,
        work_step=step,
        attempt=_Attempt(),
        box=box,
        messages=[],
        baseline=None,
        cycle=0,
        final_text=final_text,
    )
    return result, run, step


async def test_a_finished_client_attach_run_lands_a_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hole itself. A run that changed files must leave the server holding
    the same artifact contract a server-sandbox run leaves — that is what puts
    the item in the founder's approval queue and sends the telegram."""
    _stub_settle_dependencies(monkeypatch, gate=_PROVED_GATE)
    async with memory_session() as session:
        result, run, _step = await _settle(session, box=_Box(), gate=_PROVED_GATE)

        assert result is not None, "the run still settles"

        deliverable = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .one()
        )
        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload["artifact_refs"] == ["src/report.py"]

        event = (
            (
                await session.execute(
                    select(DeliveryEventRow).where(DeliveryEventRow.run_id == run.id)
                )
            )
            .scalars()
            .one()
        )
        assert event.deliverable_id == deliverable.id

        settle = (
            (
                await session.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.run_id == run.id,
                        ExecutionRunActivity.activity_type == "settle",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(settle) == 1
        assert settle[0].payload["verified"] is True


async def test_a_run_that_changed_nothing_lands_no_approval_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#735's rule, one layer up: an empty commit is a claim that something
    happened, and so is a deliverable. A run whose tree the founder's own git
    reports unchanged has nothing to show, and manufacturing an approval item
    for it trains the founder to approve without looking."""
    _stub_settle_dependencies(monkeypatch, gate=_PROVED_GATE)
    async with memory_session() as session:
        result, run, _step = await _settle(session, box=_Box(changed=()), gate=_PROVED_GATE)

        assert result is not None, "it still settles — silence, not a failure"
        assert (
            await session.execute(select(Deliverable).where(Deliverable.run_id == run.id))
        ).scalars().all() == []
        assert (
            await session.execute(select(DeliveryEventRow).where(DeliveryEventRow.run_id == run.id))
        ).scalars().all() == []


async def test_landing_a_deliverable_does_not_lift_proof_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honesty ratchet. ``finish_verified`` sets ``PROVED`` unconditionally
    because its call site has already observed a PASSED verdict. client_attach's
    call site has not: a repo that declares no toolchain is legitimately
    gateless, the run is still worth showing the founder, and it proves nothing.
    Sharing the LANDING must not import the proof claim."""
    _stub_settle_dependencies(monkeypatch, gate=None)
    async with memory_session() as session:
        _result, run, step = await _settle(session, box=_Box(), gate=None)

        assert step.proof_state is ProofState.UNTESTED, "no gate ran — nothing was proved"
        # …and the founder still sees the work.
        assert (
            await session.execute(select(Deliverable).where(Deliverable.run_id == run.id))
        ).scalars().one() is not None


async def test_the_summary_carries_the_intent_the_files_and_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the founder actually reads. The title line becomes the PR title
    (#738) and the settle note title, so it is the founder's intent — not the
    agent's raw narration. The gate that ran on their machine is woven in by the
    SAME deterministic sentence the sandbox path uses: a proof is a proof
    wherever the command ran."""
    _stub_settle_dependencies(monkeypatch, gate=_PROVED_GATE)
    async with memory_session() as session:
        _result, run, _step = await _settle(session, box=_Box(), gate=_PROVED_GATE)

        deliverable = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .one()
        )
        summary = deliverable.payload["summary"]
        assert summary.splitlines()[0].strip() == "주간 리포트를 고쳐줘"
        assert "src/report.py" in summary
        assert "2" in summary and ("checks passed" in summary or "확인 통과" in summary)
        # The raw command strings never reach a user-facing summary (F4 anti-slop).
        assert "uv run pytest" not in summary


async def test_a_missing_tool_is_never_reported_as_a_passed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control on the shape mapping. The in-place gate records exit 127
    as ``unavailable`` — the tool was not on that machine, which is honest and
    proves nothing. Counting it toward "N checks passed" would put a fabricated
    proof in front of the founder at the exact moment they are deciding whether
    to approve."""
    gate = {
        "origin": "derived_in_place",
        "applicable": True,
        "commands": [
            {"command": "uv run pytest -q", "kind": "test", "status": "passed"},
            {"command": "npx playwright test", "kind": "surface", "status": "unavailable"},
        ],
        "passed": True,
        "proved": True,
    }
    _stub_settle_dependencies(monkeypatch, gate=gate)
    async with memory_session() as session:
        _result, run, _step = await _settle(session, box=_Box(), gate=gate)

        summary = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .one()
            .payload["summary"]
        )
        assert "1" in summary
        assert "2개 확인 통과" not in summary and "2 checks passed" not in summary


async def test_the_terminal_result_carries_the_agents_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``summary=""`` was thrown away at the terminal — the one piece of the run
    only the agent could write. It is the fallback body for a deliverable with
    no file list, and it is what the caller records."""
    _stub_settle_dependencies(monkeypatch, gate=_PROVED_GATE)
    async with memory_session() as session:
        result, _run, _step = await _settle(
            session, box=_Box(), gate=_PROVED_GATE, final_text="리포트 생성 버그를 고쳤어요."
        )

        assert result.summary == "리포트 생성 버그를 고쳤어요."


async def test_both_execution_models_land_through_the_same_helper() -> None:
    """One contract, one implementation. The moment the two backends grow their
    own landing code, they drift — and the drift shows up as a downstream
    consumer (DeliveryWorker, SettleWorker, the Brief) that silently handles one
    kind of run and not the other. That is the shape this whole hole had."""
    import inspect

    from backend.workflow.application import client_attach_delivery, run_persistence

    assert hasattr(run_persistence, "land_verified_artifacts")
    assert "land_verified_artifacts" in inspect.getsource(run_persistence.finish_verified)
    assert "land_verified_artifacts" in inspect.getsource(
        client_attach_delivery.land_client_attach_deliverable
    )


class _GitBox:
    """The founder's machine, scripting the git ladder's decision points."""

    def __init__(self, *, staged: bool, ahead: int) -> None:
        self.commands: list[str] = []
        self._staged = staged
        self._ahead = ahead

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> Any:
        from backend.workflow.infrastructure.sandbox import SandboxResult

        self.commands.append(command)
        if command.startswith("git diff --cached --quiet"):
            # exit 0 = NOTHING staged.
            return SandboxResult(
                exit_code=1 if self._staged else 0, stdout="", stderr="", timed_out=False
            )
        if command.startswith("git rev-list --count"):
            return SandboxResult(exit_code=0, stdout=f"{self._ahead}\n", stderr="", timed_out=False)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)


async def test_commits_the_agent_made_itself_are_still_pushed() -> None:
    """ "Nothing to commit" is not "nothing to push".

    The merge-conflict directive tells the agent to COMMIT the resolution
    itself, so on that path this step finds an empty index — and #735 returned
    right there, before the push. Live run ``0ef554ca``: the agent resolved the
    conflict and made a real merge commit; the branch sat three commits ahead on
    the founder's machine, github never saw it, the PR stayed unmergeable, and
    the same conflict was re-dispatched until it escalated.
    """
    from backend.workflow.application.client_attach_delivery import commit_and_push_run_work

    box = _GitBox(staged=False, ahead=3)
    run = SimpleNamespace(id=uuid.uuid4(), payload={"intent_text": "x"})

    outcome = await commit_and_push_run_work(box=box, run=run, baseline="abc1234")

    assert outcome.pushed is True
    assert outcome.committed is False, "this step made no commit — the agent did"
    assert any(c.startswith("git push") for c in box.commands)


async def test_a_run_that_produced_no_commit_at_all_still_pushes_nothing() -> None:
    """#735's rule survives: a branch with no commits of its own is not pushed.
    Otherwise every no-op run leaves a remote branch claiming work happened."""
    from backend.workflow.application.client_attach_delivery import commit_and_push_run_work

    box = _GitBox(staged=False, ahead=0)
    run = SimpleNamespace(id=uuid.uuid4(), payload={"intent_text": "x"})

    outcome = await commit_and_push_run_work(box=box, run=run, baseline="abc1234")

    assert outcome.pushed is False
    assert not any(c.startswith("git push") for c in box.commands)


async def test_without_a_baseline_an_empty_index_is_still_not_a_push() -> None:
    """No baseline means no way to ask "did this run produce commits", and a
    guess either way is worse than the honest answer. Fail closed, as #735 did."""
    from backend.workflow.application.client_attach_delivery import commit_and_push_run_work

    box = _GitBox(staged=False, ahead=3)
    run = SimpleNamespace(id=uuid.uuid4(), payload={"intent_text": "x"})

    outcome = await commit_and_push_run_work(box=box, run=run, baseline=None)

    assert outcome.pushed is False
