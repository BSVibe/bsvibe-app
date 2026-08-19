"""An ASK run takes the SAME tool-surface seam as any other run.

prod 실측 (`c40c513d`, 2026-08-19): *"네 경로 각각 코드로 확인하고 근거 파일:라인을
대라"* 가 `knowledge_only` 로 분류돼 :class:`KnowledgeAnswerOrchestrator` 로 갔고,
그 오케스트레이터는 **툴이 0개**라 레포를 열어보지 못한 채 **추측으로 답했다**
(산출물이 스스로 *"코드를 직접 열람한 것이 아님을 먼저 밝힙니다"* 로 시작한다).

원인은 라우팅 축의 부족이 아니다. 툴 표면을 정하는 자리는
``tool_registry.mcp_tool_names_for`` 하나이고 ``RUN_TOOL_FORWARDING`` 이
*"the SINGLE source of truth"* (INV-7)인데, `knowledge_only` 만 **그 seam 을 아예
우회**했다. 굶은 에이전트는 실패하지 않고 추측한다.

∴ 우회를 없앤다. 워크스페이스 툴은 조건 없이 준다 — 런은 이미 워크스페이스에 묶여
있고, *"이 요청이 파일을 봐야 하나"* 는 프레임이 미리 맞힐 수 있는 것이 아니라
에이전트가 열어봐야 아는 것이다.

남는 위험은 읽기가 아니라 **쓰기**다 — prod `ff1615e8` (*"현 프로젝트 상황
설명해줘"*)이 루프로 가서 **무관한 diff 를 shipped** 했다. 그래서 우회 제거와
ASK 지시문은 **같은 변경의 두 반쪽**이고 함께 간다: 툴은 주되, 만들지 말라고 말한다.
(`_DESIGN_SPEC_DIRECTIVE` 가 design 단계에 쓰는 것과 같은 seam.)
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.application._loop_context import ask_directive_message
from backend.workflow.application.agent_briefing import _ASK_ANSWER_DIRECTIVE
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

pytestmark = pytest.mark.asyncio


def _run(classification: str | None, *, pipeline: str = "single") -> ExecutionRun:
    frame: dict[str, str] = {"pipeline": pipeline}
    if classification is not None:
        frame["path_classification"] = classification
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": "코드로 확인하고 근거 파일:라인을 대라", "frame": frame},
    )


async def test_ask_run_is_told_to_answer_not_produce() -> None:
    """An ASK run gets the directive that keeps it from shipping a diff.

    This is the `ff1615e8` guard: the tools are no longer withheld, so the
    *instruction* is what separates an answer from a code change.
    """
    message = ask_directive_message(_run("knowledge_only"))

    assert message is not None
    assert message["role"] == "system"
    assert message["content"] == _ASK_ANSWER_DIRECTIVE


async def test_produce_run_gets_no_ask_directive() -> None:
    """An `agent_loop` (PRODUCE) run is untouched — it is supposed to build."""
    assert ask_directive_message(_run("agent_loop")) is None


async def test_run_without_a_frame_gets_no_ask_directive() -> None:
    """No frame → loop unchanged (same contract as ``design_directive_message``)."""
    assert ask_directive_message(_run(None)) is None
    run = _run("agent_loop")
    run.payload = {"intent_text": "no frame at all"}
    assert ask_directive_message(run) is None


async def test_directive_forbids_producing_and_demands_grounding() -> None:
    """The directive must carry BOTH halves, or it fails one of the two prod runs.

    * `ff1615e8` — a question shipped a diff → it must forbid producing.
    * `c40c513d` — an answer was guessed → it must demand the files be read.
    """
    text = _ASK_ANSWER_DIRECTIVE.lower()

    assert "file_read" in text
    assert "declare_verification" in text
    # No deliverable-producing verbs are invited.
    assert "do not" in text or "never" in text
