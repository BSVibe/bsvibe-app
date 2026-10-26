"""The ledger is built at framing and shown to the run — through the real worker drive.

``drive_once`` frames the request (→ ``run.payload["requirements"]``) and drives the loop
(→ the first-turn context carries the ledger). Nothing here calls the ledger functions.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.extensions.skill.loader import SkillLoader
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.application.stages.frame import TextCompletion
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker

from .._support import db_engine
from .test_frame_consumed import _RecordingLlm, _seed_request_and_run

pytestmark = pytest.mark.asyncio

_REQUEST = "리포트 요청\n\n## 할 것\n- [ ] 집계 스크립트를 만든다\n- [ ] 결과를 표로 보여 준다\n"


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _FrameLlm:
    async def complete_text(self, *, system: str, user: str) -> TextCompletion:
        return TextCompletion(
            text=json.dumps(
                {
                    "framed_intent": "build a report",
                    "skill_match": None,
                    "artifact_type_hint": "code",
                    "path_classification": "agent_loop",
                    "pipeline": "single",
                }
            )
        )


async def _drive(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path, text: str
) -> tuple[uuid.UUID, uuid.UUID, _RecordingLlm]:
    workspace_id = uuid.uuid4()
    async with sf() as session, workspace_session_scope(session, workspace_id):
        run_id = await _seed_request_and_run(session, workspace_id=workspace_id, text=text)
        await session.commit()
    llm = _RecordingLlm()

    def _orchestrator_factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator:
        return RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())

    deps = AgentExecutionDeps(
        skill_loader_for=lambda ws: SkillLoader(tmp_path / "skills" / str(ws)),
        orchestrator_factory=_orchestrator_factory,
        workspace_root=tmp_path / "runs",
        frame_llm=_FrameLlm(),
    )
    assert await AgentWorker(session_factory=sf, execution=deps).drive_once() == 1
    return workspace_id, run_id, llm


async def test_framing_fills_the_requirements_of_the_run(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id, run_id, _ = await _drive(sf, tmp_path, _REQUEST)

    async with sf() as session, workspace_session_scope(session, workspace_id):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        items: list[dict[str, Any]] = run.payload["requirements"]
    assert [i["text"] for i in items] == ["집계 스크립트를 만든다", "결과를 표로 보여 준다"]
    assert {i["kind"] for i in items} == {"do"}
    assert all(i["id"] for i in items)


async def test_the_run_sees_its_ledger_in_the_first_turn(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _, _, llm = await _drive(sf, tmp_path, _REQUEST)

    blob = "\n".join(
        m["content"] for m in llm.calls[0]["messages"] if isinstance(m.get("content"), str)
    )
    assert "REQUIREMENTS LEDGER" in blob
    assert "집계 스크립트를 만든다" in blob
    assert "requirement_evidence" in blob


# ── where v1 does NOT build a ledger ─────────────────────────────────────────


def _framed(path: str, hint: str | None) -> Any:
    from backend.workflow.application.stages.frame import FramedRequest

    return FramedRequest(skill_match=None, artifact_type_hint=hint, path_classification=path)


@pytest.mark.parametrize(
    ("path", "hint", "client_attach", "applies"),
    [
        ("agent_loop", "code", False, True),
        ("agent_loop", "pr", False, True),
        ("agent_loop", "code", True, False),  # settles on the founder's machine
        ("agent_loop", "direct_output", False, False),  # prose has no diff to cite
        ("knowledge_only", "code", False, False),
    ],
)
async def test_the_ledger_is_built_only_where_the_server_gate_judges_it(
    monkeypatch: pytest.MonkeyPatch, path: str, hint: str, client_attach: bool, applies: bool
) -> None:
    from backend.workflow.application import _loop_context
    from backend.workflow.infrastructure.workers.agent_worker import _ledger_applies

    async def _is_client_attach(session: Any, run: Any) -> bool:
        return client_attach

    monkeypatch.setattr(_loop_context, "is_client_attach_run", _is_client_attach)
    assert await _ledger_applies(None, None, _framed(path, hint)) is applies  # type: ignore[arg-type]


async def test_a_request_with_no_structure_gets_no_ledger(
    sf: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id, run_id, llm = await _drive(sf, tmp_path, "리포트를 좀 더 낫게 만들어 줘")

    async with sf() as session, workspace_session_scope(session, workspace_id):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        assert "requirements" not in run.payload
    blob = "\n".join(
        m["content"] for m in llm.calls[0]["messages"] if isinstance(m.get("content"), str)
    )
    assert "REQUIREMENTS LEDGER" not in blob
