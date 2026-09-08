"""Framing is the platform's single most common terminal failure, and it was a
dead end.

Measured on prod 2026-09-08: of the 24 ``failed`` notifications BSVibe has ever
sent, **21** carry the body ``"frame could not classify the request: ..."``.
Framing is one cheap-LLM call with no retry anywhere under it, so a single
transient blip ended the run.

The founder WAS told — ``AgentRunner.transition(FAILED)`` emits the ``failed``
outbox row and it was ``sent`` 1.7s later (prod row ``be115856``, 2026-09-07
10:05:58). What they were told is the problem:

* it is written for engineers — "frame could not classify the request: the frame
  model call failed" names an internal stage, an internal verdict and an
  internal model role, none of which the founder chose or has ever seen
  (§Ⅳ.b 규율 145: the reader is the axis);
* it is terminal, so it carries no action. FAILED is not in the checkpoint
  queue, has no ``Try again`` button, and nothing re-drives it. The one thing
  that has ever recovered such a run is the founder (or me) calling
  ``runs_retry`` by hand — and on 2026-09-07 that manual retry passed the SAME
  prompt through the SAME code first try.

So a frame that cannot classify is not a verdict about the request. It is a
drive that did not start, which is a condition the worker already handles
correctly: retry it a bounded number of times, and when the bound says this is
not going to fix itself, raise the ``run_drive_failed`` Decision — founder
language, ``Try again`` / ``Discard``, in the checkpoint queue.

``FrameModelUnresolvedError`` keeps its own branch: no model is routed, so
retrying re-runs the same missing configuration. It is a subclass, so the
ordering of the two ``except`` clauses is itself load-bearing and pinned below.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.notifications.db  # noqa: F401 — register the table on the shared Base
from backend.extensions.skill.loader import SkillLoader
from backend.notifications.db import NotificationEventRow
from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import (
    DRIVE_FAILED_KIND,
    AgentExecutionDeps,
    AgentWorker,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _EndsTheLoopLlm:
    """A ``LoopLlm`` that ends the loop immediately with prose (no work)."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls += 1
        return LoopTurn(content="nothing to do", tool_calls=())


class _FlakyFrameLlm:
    """A ``FrameLlm`` that raises for its first ``failures`` calls, then frames.

    This is the shape prod actually produced: the same prompt, the same code, a
    different answer (handoff 2026-09-07 §Ⅳ.a, and prod rows on 2026-09-04 and
    2026-09-07 with two different frame-stage messages)."""

    def __init__(self, failures: int) -> None:
        self._remaining = failures
        self.calls = 0

    async def complete_text(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("upstream 503")
        return json.dumps(
            {
                "framed_intent": "build a thing",
                "skill_match": None,
                "artifact_type_hint": "code",
                "path_classification": "agent_loop",
                "pipeline": "single",
            }
        )


async def _seed_request_and_run(
    session: AsyncSession, *, workspace_id: uuid.UUID, text: str
) -> uuid.UUID:
    trigger = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source="direct",
        trigger_kind=TriggerKind.DIRECT,
        idempotency_key=f"k-{uuid.uuid4()}",
        payload={"text": text},
        received_at=datetime.now(tz=UTC),
    )
    session.add(trigger)
    await session.flush()
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trigger_event_id=trigger.id,
        status=RequestStatus.RUNNING,
        payload={"text": text},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(request)
    await session.flush()
    return await AgentRunner(session).open_run(request=request)


def _deps(tmp_path: Path, frame_llm: Any, loop_llm: Any) -> AgentExecutionDeps:
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(tmp_path / "skills" / str(ws_id))
        loader.load_all()
        return loader

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        return RunOrchestrator(session=session, llm=loop_llm, sandbox_manager=NoopSandboxManager())

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_orchestrator_factory,
        workspace_root=tmp_path / "runs",
        frame_llm=frame_llm,
    )


def _worker(sf: Any, deps: AgentExecutionDeps, *, max_drive_failures: int) -> AgentWorker:
    from backend.config import get_settings

    settings = get_settings().model_copy(update={"agent_max_drive_failures": max_drive_failures})
    return AgentWorker(session_factory=sf, execution=deps, settings=settings)


async def _decisions(session: AsyncSession, run_id: uuid.UUID) -> list[Decision]:
    rows = await session.execute(select(Decision).where(Decision.run_id == run_id))
    return list(rows.scalars().all())


async def test_a_transient_framing_blip_is_retried_and_the_run_still_lands(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The measured prod case. One frame call failed; the next one, on the same
    prompt, classified fine. That run must reach the founder as WORK, not as a
    failure notice they have to hand-retry."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build a thing"
        )
        await session.commit()

    frame_llm = _FlakyFrameLlm(failures=1)
    loop_llm = _EndsTheLoopLlm()
    worker = _worker(sf, _deps(tmp_path, frame_llm, loop_llm), max_drive_failures=3)

    assert await worker.drive_once() == 0, "the blip is not a driven run"
    assert await worker.drive_once() == 1, "the retry must re-frame and drive"

    assert frame_llm.calls == 2, "the frame stage must be re-entered on the retry"
    assert loop_llm.calls > 0, "the run reached the agent loop"

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is not RunStatus.FAILED, "a transient blip must not be terminal"
        assert (run.payload or {}).get("frame"), "the retry recorded the framing"
        assert await _decisions(session, run_id) == [], "nothing needed the founder"


async def test_a_framing_failure_never_ends_the_run_in_silence_or_jargon(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Past the bound the founder is called — with the ``run_drive_failed``
    Decision, which is in the checkpoint queue and carries ``Try again`` /
    ``Discard``. NOT with a terminal FAILED whose only content is the frame
    stage's own English error string."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build a thing"
        )
        await session.commit()

    frame_llm = _FlakyFrameLlm(failures=99)
    worker = _worker(sf, _deps(tmp_path, frame_llm, _EndsTheLoopLlm()), max_drive_failures=2)

    await worker.drive_once()
    await worker.drive_once()

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is not RunStatus.FAILED, (
            "FAILED is terminal: no queue, no button, nothing re-drives it"
        )
        assert run.claimed_at is None, "a run paused on a Decision holds no claim"

        decisions = await _decisions(session, run_id)
        assert [d.decision for d in decisions] == [DRIVE_FAILED_KIND]
        assert decisions[0].status is DecisionStatus.PENDING

        bodies = [
            r.payload.get("body", "")
            for r in (
                await session.execute(
                    select(NotificationEventRow).where(
                        NotificationEventRow.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        ]
        assert bodies, "the founder must still be called"
        for body in bodies:
            assert "frame" not in body.lower(), f"internal stage name reached the founder: {body!r}"


async def test_an_unresolved_frame_model_keeps_its_own_branch(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``FrameModelUnresolvedError`` is a SUBCLASS of ``FrameUnclassifiedError``,
    so the two ``except`` clauses' order is load-bearing. No model routed means
    retrying re-runs the same missing configuration — it pauses the run on the
    model-account Decision instead, and must not be counted as a drive failure."""
    workspace_id = uuid.uuid4()
    async with sf() as session:
        run_id = await _seed_request_and_run(
            session, workspace_id=workspace_id, text="build a thing"
        )
        await session.commit()

    # frame_llm=None → the stage raises FrameModelUnresolvedError.
    worker = _worker(sf, _deps(tmp_path, None, _EndsTheLoopLlm()), max_drive_failures=1)
    await worker.drive_once()

    async with sf() as session:
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.RUNNING
        kinds = [d.decision for d in await _decisions(session, run_id)]
        assert DRIVE_FAILED_KIND not in kinds, (
            "an unrouted model is a configuration Decision, not a crashed drive"
        )


def _exception_names_caught(source: str) -> set[str]:
    """Every exception name that appears in an ``except`` clause in ``source``.

    Parsed, not grepped. The first version of this guard was a substring search
    for ``"FrameUnclassifiedError"``, and it went red on the COMMENT that
    explains why the branch is gone — a guard that reads prose measures nothing
    about the code (`absence-guard-listing-spellings-proves-only-imagination`)."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        for ref in ast.walk(node.type):
            if isinstance(ref, ast.Name):
                names.add(ref.id)
            elif isinstance(ref, ast.Attribute):
                names.add(ref.attr)
    return names


async def test_the_absence_guard_can_actually_fail() -> None:
    """The control. An absence assertion that has never been shown to go red is
    indistinguishable from one that cannot — so hand the same reader the branch
    this file deleted and require it to be seen."""
    caught = _exception_names_caught(
        "try:\n"
        "    frame()\n"
        "except FrameModelUnresolvedError:\n"
        "    pass\n"
        "except frame_module.FrameUnclassifiedError as exc:\n"
        "    fail(exc)\n"
    )
    assert "FrameUnclassifiedError" in caught
    assert "FrameModelUnresolvedError" in caught


async def test_no_path_fails_a_run_for_an_unclassifiable_frame() -> None:
    """The absence guard. Pinned to the PROPOSITION — "the worker catches no
    unclassifiable frame" — so re-introducing the branch fails here however it
    words its reason, and rewording a comment does not.

    ``FrameModelUnresolvedError`` is a subclass, so it must still be caught:
    asserting only the absence would be satisfied by deleting both."""
    from backend.workflow.infrastructure.workers import agent_worker

    caught = _exception_names_caught(Path(agent_worker.__file__).read_text(encoding="utf-8"))

    assert "FrameUnclassifiedError" not in caught, (
        "the worker must not special-case an unclassifiable frame — it is a "
        "drive that did not start, and _on_drive_failed already owns that"
    )
    assert "FrameModelUnresolvedError" in caught, (
        "the unrouted-model branch is the one that must stay: retrying it just "
        "re-runs the same missing configuration"
    )
