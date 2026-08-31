"""조사만 하는 런은 "일을 안 했다"고 다그쳐지면 안 된다.

형님 판정 2026-08-20: *"검증은 통과/실패 둘 뿐이야 오직. … 정말 검증할게 없어서
아무것도 안한거는 통과야."*

그 판정은 :mod:`~backend.workflow.application.undeclared_verification` 에는
적용됐다 — 바뀐 게 없으면 ``PASSED`` 를 기록하고 통과시킨다. 그런데 **같은 판정이
:mod:`~backend.workflow.application._drive_loop` 의 no-work nudge 에는 적용되지
않았다.** 한 판정, 두 지점, 한 곳만 갱신된 것이다.

그 nudge 는 이렇게 말한다:

    "You have not changed any file or declared a verification contract yet.
     **A prose answer is not a deliverable.** Use the tools to do the work, then
     declare_verification, then summarise."

prod 실측(2026-08-24 · 08-25 · 08-31 ×2) — 형님이 *"조사만 하고 보고해라 — 파일은
하나도 쓰지 마라"* 로 스코프한 런에서, 에이전트는 도구로 실제 조사를 마치고
정확한 답을 냈는데도 이 메시지를 최대 3회(``MAX_NO_WORK_NUDGES`` + 1) 받았다.
한 번은 결국 형님께 질문을 올려 런이 멈췄다.

세 가지가 겹친다:

1. 그 런들의 ``writes`` 는 **전부 비어 있었다** — 에이전트는 정말 아무것도 안 썼고,
   그게 지시받은 바였다.
2. nudge 는 대화 **맨 뒤**에 붙는다. 뒤에 붙은 구체적 문장이 앞의 지시를 이긴다
   (#779/#781) — 그래서 *"산문은 산출물이 아니다"* 가 형님의 *"보고해라"* 를 덮는다.
3. 루프에 **"조사만 하는 작업"이라는 개념이 없다.** 모든 런이 파일을 고쳐야 한다고
   가정한다.

고치는 것은 nudge 를 없애는 게 아니다 — *"아무 도구도 안 쓰고 산문만 뱉는"* 진짜
게으름은 계속 막아야 한다(이 파일 맨 아래 양성 대조군). 고치는 것은 **판정을 두
번째 지점에도 적용**하는 것이다: 파일을 안 고친 것과 일을 안 한 것은 다르다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.db import Decision
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

from ..execution.test_run_orchestrator import ScriptedLlm, _make_run, _tc

NUDGE_MARK = "You have not used any tool yet"


def _nudges(llm: ScriptedLlm) -> list[str]:
    """no-work nudge 가 대화에 실린 횟수 — 메시지를 직접 본다."""
    seen: list[str] = []
    for call in llm.calls:
        for message in call["messages"]:
            content = message.get("content")
            if isinstance(content, str) and NUDGE_MARK in content and content not in seen:
                seen.append(content)
    return seen


class TestAnInvestigationRunIsLeftAlone:
    @pytest.mark.asyncio
    async def test_a_read_only_run_is_never_nudged(self, tmp_path: Path) -> None:
        """도구로 조사하고 산문으로 답한 런은 다그쳐지지 않는다.

        스크립트가 딱 2턴이다 — nudge 가 붙으면 루프가 3번째 턴을 요구하고
        ``ScriptedLlm`` 이 소진되어 터진다. 즉 이 테스트는 "메시지가 없다"만이
        아니라 **런이 더 돌지 않는다**까지 고정한다.
        """
        (tmp_path / "target.py").write_text("x = 1\n", encoding="utf-8")
        llm = ScriptedLlm(
            [
                LoopTurn(content="", tool_calls=(_tc("file_read", path="target.py"),)),
                LoopTurn(content="한 문장 보고: x 는 1 로 초기화됩니다.", tool_calls=()),
            ]
        )
        async with memory_session() as session:
            run = await _make_run(session)
            orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
            result = await orch.run(run=run, workspace_dir=tmp_path)

            assert _nudges(llm) == [], "조사한 에이전트를 '일을 안 했다'고 다그치면 안 된다"
            assert result.outcome == "verified", "바꾼 게 없으면 통과 — 제3상태는 없다"
            assert (await session.execute(select(Decision))).scalars().all() == [], (
                "형님을 부르지 않는다"
            )

    @pytest.mark.asyncio
    async def test_a_shell_only_investigation_counts_as_work(self, tmp_path: Path) -> None:
        """``shell_exec`` 만 쓴 조사도 일이다.

        ``_grounded_paths`` 는 ``file_read``/``file_write`` 만 채우므로 그것으로
        판정하면 grep·find 로만 조사한 런이 여전히 다그쳐진다. 판정은 **도구를
        썼는가**여야 한다.
        """
        llm = ScriptedLlm(
            [
                LoopTurn(content="", tool_calls=(_tc("shell_exec", command="echo hello"),)),
                LoopTurn(content="한 문장 보고: 라우팅 규칙은 3개입니다.", tool_calls=()),
            ]
        )
        async with memory_session() as session:
            run = await _make_run(session)
            orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
            result = await orch.run(run=run, workspace_dir=tmp_path)

            assert _nudges(llm) == []
            assert result.outcome == "verified"


class TestTheSignalSurvivesTheTransport:
    """MCP 트랜스포트는 **요청마다** 레지스트리를 새로 만든다.

    그래서 "일을 했는가" 신호가 ``export_state``/``restore_state`` 를 타지 못하면,
    executor 런은 매 요청 0 호출로 보여 계속 다그쳐진다 — 계약이 이미 같은 이유로
    ``declared_contract`` 와 ``written_paths`` 를 싣고 있다.

    ⚠️ 이 클래스는 음성 대조군이 잡아내서 추가됐다: export 에서 카운터를 빼도
    루프 테스트는 전부 green 이었다. 배선의 절반이 무방비였다.
    """

    @pytest.mark.asyncio
    async def test_the_call_count_rides_export_and_restore(self, tmp_path: Path) -> None:
        from backend.workflow.infrastructure.tools import ToolRegistry

        (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
        first = ToolRegistry(workspace_dir=tmp_path)
        await first.invoke("file_read", {"path": "a.txt"})
        assert first.succeeded_tool_calls == 1

        second = ToolRegistry(workspace_dir=tmp_path)
        second.restore_state(first.export_state())

        assert second.succeeded_tool_calls == 1, (
            "요청마다 새 레지스트리를 만드는 트랜스포트에서 신호가 사라지면 "
            "executor 런은 영원히 '일을 안 했다'로 보인다"
        )

    @pytest.mark.asyncio
    async def test_restore_never_lowers_the_count(self, tmp_path: Path) -> None:
        """복원이 이미 센 것을 깎으면 안 된다 — 단조 증가."""
        from backend.workflow.infrastructure.tools import ToolRegistry

        (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
        registry = ToolRegistry(workspace_dir=tmp_path)
        await registry.invoke("file_read", {"path": "a.txt"})
        await registry.invoke("file_read", {"path": "a.txt"})

        registry.restore_state({"succeeded_tool_calls": 0})

        assert registry.succeeded_tool_calls == 2


class TestTheGuardAgainstThinAirStillHolds:
    """⚠️ 양성 대조군 — 이걸 깨뜨리면 안 된다.

    nudge 의 존재 이유는 *아무것도 안 하고 산문만 뱉는* 것을 막는 것이다. 그건
    계속 막혀야 한다.
    """

    @pytest.mark.asyncio
    async def test_prose_without_any_tool_call_is_still_nudged(self, tmp_path: Path) -> None:
        """도구를 한 번도 안 쓰고 답만 하면 여전히 다그쳐진다."""
        llm = ScriptedLlm(
            [
                LoopTurn(content="제 생각엔 다 된 것 같습니다", tool_calls=()),
                LoopTurn(content="여전히 다 된 것 같습니다", tool_calls=()),
                LoopTurn(content="정말 다 됐습니다", tool_calls=()),
            ]
        )
        async with memory_session() as session:
            run = await _make_run(session)
            orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
            await orch.run(run=run, workspace_dir=tmp_path)

            assert _nudges(llm), "도구를 하나도 안 썼으면 다그쳐야 한다"

    @pytest.mark.asyncio
    async def test_a_refused_write_is_not_work(self, tmp_path: Path) -> None:
        """B7 게이트에 **거부당한** 쓰기는 일이 아니다 — 계속 다그쳐야 한다.

        거부된 호출을 '도구를 썼다'로 세면, 선언 없이 쓰려다 막힌 에이전트가
        그 실패로 nudge 를 면제받는다. 정확히 게이트가 막으려던 것이다.
        """
        llm = ScriptedLlm(
            [
                LoopTurn(
                    content="", tool_calls=(_tc("file_write", path="foo.txt", content="bar"),)
                ),
                LoopTurn(content="다 됐습니다", tool_calls=()),
                LoopTurn(content="정말 다 됐습니다", tool_calls=()),
                LoopTurn(content="포기합니다", tool_calls=()),
            ]
        )
        async with memory_session() as session:
            run = await _make_run(session)
            orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
            await orch.run(run=run, workspace_dir=tmp_path)

            assert _nudges(llm), "거부당한 쓰기는 일이 아니다"
            assert not (tmp_path / "foo.txt").exists()
