"""An issue edit reaches the running agent — sent during framing, or between two turns.

Prod #1293 (run 94e04ab3, 2026-11-03): an edit routed 12 s after the run opened was written
over by framing's stale copy of the run; a second one, routed mid-drive, was written over by
the drive — and the drive only read routed edits when a drive started anyway.

Driven through the real ``AgentWorker.drive_once``; each edit is routed by the real
``route_issue_edit_to_run`` in its own session, while the frame call / a turn is in flight.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.application.intake.issue_edit import route_issue_edit_to_run
from backend.workflow.application.stages.frame import TextCompletion
from backend.workflow.domain.requirements import REQUIREMENTS_KEY
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker
from plugin.github.webhook import parse_webhook

from .._support import db_engine

REPO = "acme/app"
_V1 = "## 할 것\n- [ ] 통과율 기본값은 20 이다\n"
_V2 = "## 할 것\n- [ ] 통과율 기본값은 30 이다\n"


def _issue_event(body: str, action: str) -> dict[str, Any]:
    raw = json.dumps(
        {
            "action": action,
            "issue": {
                "number": 1293,
                "title": "통과율",
                "body": body,
                "user": {"login": "founder"},
                "author_association": "OWNER",
            },
            "repository": {"full_name": REPO},
            "sender": {"login": "founder", "type": "User"},
        }
    ).encode()
    trig = parse_webhook(
        workspace_id=uuid.uuid4(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": uuid.uuid4().hex},
        raw_body=raw,
    )
    assert trig is not None
    return dict(trig.payload)


class _ActLlm:
    """The act loop. With ``edit_on_first_turn`` it routes an edit while turn 1 is in flight
    and calls a tool (so there is a turn 2); every turn's messages are kept."""

    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession] | None = None,
        workspace_id: uuid.UUID | None = None,
        *,
        done_on_first_turn: bool = False,
    ) -> None:
        self._sf, self._ws, self._done_first = sf, workspace_id, done_on_first_turn
        self.turns: list[list[str]] = []

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
        self.turns.append([str(m.get("content") or "") for m in messages if isinstance(m, dict)])
        n = len(self.turns)
        # ``done_on_first_turn``: turn 1 works (a tool call); turn 2 is the long turn the edit
        # lands in, and it ends "done" — an executor's one-session shape.
        edit_turn = 2 if self._done_first else 1
        if self._sf is not None and n == edit_turn:
            async with self._sf() as session:
                await route_issue_edit_to_run(
                    session, workspace_id=self._ws, payload=_issue_event(_V2, "edited")
                )
                await session.commit()
            if self._done_first:
                return LoopTurn(content="done", tool_calls=())
        if n == 1:
            return LoopTurn(
                content="",
                tool_calls=(LoopToolCall(id="t1", name="no_such_tool", arguments={}),),
            )
        return LoopTurn(content="done", tool_calls=())


class _EditingFrameLlm:
    """Frames the request — and, mid-call, lets intake route the founder's edit."""

    def __init__(
        self, sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID, *, edit: bool
    ) -> None:
        self._sf, self._ws, self._edit = sf, workspace_id, edit
        self.routed: uuid.UUID | None = None

    async def complete_text(self, *, system: str, user: str) -> TextCompletion:
        if self._edit:
            async with self._sf() as session:
                self.routed = await route_issue_edit_to_run(
                    session, workspace_id=self._ws, payload=_issue_event(_V2, "edited")
                )
                await session.commit()
        body = json.dumps(
            {
                "framed_intent": "통과율",
                "skill_match": None,
                "artifact_type_hint": "code",
                "path_classification": "agent_loop",
            }
        )
        return TextCompletion(text=body, usage_prompt_tokens=1, usage_completion_tokens=1)


async def _drive(
    tmp_path: Path, *, edit_while_framing: bool, done_on_first_turn: bool = False
) -> tuple[_ActLlm, dict[str, Any]]:
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws = uuid.uuid4()
        opened = _issue_event(_V1, "opened")
        async with sf() as session, workspace_session_scope(session, ws):
            trigger = TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                source="github",
                trigger_kind=TriggerKind.WEBHOOK,
                idempotency_key=f"k-{uuid.uuid4()}",
                payload=opened,
            )
            session.add(trigger)
            await session.flush()
            request = RequestRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                trigger_event_id=trigger.id,
                status=RequestStatus.RUNNING,
                payload=opened,
            )
            session.add(request)
            await session.flush()
            run_id = await AgentRunner(session).open_run(request=request)
            await session.commit()

        def _loader(w: uuid.UUID) -> SkillLoader:
            loader = SkillLoader(tmp_path / "skills" / str(w))
            loader.load_all()
            return loader

        act = (
            _ActLlm()
            if edit_while_framing
            else _ActLlm(sf, ws, done_on_first_turn=done_on_first_turn)
        )
        agent = AgentWorker(
            session_factory=sf,
            execution=AgentExecutionDeps(
                skill_loader_for=_loader,
                orchestrator_factory=lambda s, r: RunOrchestrator(
                    session=s,
                    llm=act,
                    sandbox_manager=NoopSandboxManager(),
                    skill_loader=_loader(r.workspace_id),
                ),
                workspace_root=tmp_path / "runs",
                frame_llm=_EditingFrameLlm(sf, ws, edit=edit_while_framing),
            ),
        )
        await agent.drive_once()

        async with sf() as session, workspace_session_scope(session, ws):
            run = await session.get(ExecutionRun, run_id)
            assert run is not None
            return act, dict(run.payload or {})


def _live(payload: dict[str, Any]) -> list[str]:
    return [i["text"] for i in payload[REQUIREMENTS_KEY] if not i.get("withdrawn")]


async def test_an_edit_sent_while_framing_reaches_the_first_turn(tmp_path: Path) -> None:
    act, payload = await _drive(tmp_path, edit_while_framing=True)

    assert any("edited the issue" in m for m in act.turns[0])
    [ledger] = [m for m in act.turns[0] if "REQUIREMENTS LEDGER" in m]
    assert "기본값은 30" in ledger and "기본값은 20" not in ledger.split("withdrawn")[0]
    assert "30" in payload["intent_text"] and "20" not in payload["intent_text"]
    assert _live(payload) == ["통과율 기본값은 30 이다"]


async def test_an_edit_sent_mid_drive_reaches_the_next_turn(tmp_path: Path) -> None:
    act, payload = await _drive(tmp_path, edit_while_framing=False)

    assert not any("edited the issue" in m for m in act.turns[0])
    assert any("edited the issue" in m for m in act.turns[1])
    assert "30" in payload["intent_text"]
    assert _live(payload) == ["통과율 기본값은 30 이다"]


async def test_an_edit_sent_during_a_turn_that_ends_done_is_told_before_verifying(
    tmp_path: Path,
) -> None:
    """Prod #1296 (run 17df4e07): an executor works in ONE long turn. A comment landed in it,
    the turn ended "done", and the drive went straight to verification — the comment was never
    applied, its checklist item never reached the ledger. The drive now looks at the inbox
    before verifying, and gives the agent another turn when something new is there."""
    act, payload = await _drive(tmp_path, edit_while_framing=False, done_on_first_turn=True)

    assert len(act.turns) >= 3
    after_done = act.turns[2]
    assert any("edited the issue" in m for m in after_done)
    # The inbox is read FIRST: no nudge, no evidence reminder, no verification until the agent
    # has seen what arrived — new ledger items change what is owed.
    assert not any("REQUIREMENT EVIDENCE MISSING" in m for m in after_done)
    assert not any("You have not used any tool yet" in m for m in after_done)
    assert _live(payload) == ["통과율 기본값은 30 이다"]
