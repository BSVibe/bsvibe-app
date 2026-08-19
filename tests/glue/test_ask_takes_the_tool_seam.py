"""An ASK run takes the SAME tool-surface seam — and is told WHO IT IS, not just
what not to do.

prod 실측 `c40c513d` (2026-08-19): *"네 경로 각각 코드로 확인하고 근거 파일:라인을
대라"* 가 `knowledge_only` 로 분류돼 툴이 0개인 오케스트레이터로 갔고, 레포를 열지
못한 채 **추측으로 답했다**. #778 이 그 우회를 없앴고, 실측으로 툴 호출 0 → 89 가
됐다 — 읽기 절반은 해결됐다.

쓰기 절반은 **실패했다.** #778 은 기반 시스템 프롬프트는 그대로 둔 채
*"바꾸지 마라"* 지시문을 뒤에 덧붙였다. 그 프롬프트의 첫 문장은

    "You are an autonomous engineer … Use the tools to inspect and CHANGE FILES."

prod 실측 `fae09a47`: 지시문은 실제로 부착됐고(`ask_directive_seeded`), 형님의 작업
지시 자체도 *"파일은 하나도 쓰지 마라"* 였는데, 런은 파일 4개를 고쳐 커밋했다
(+108 −2 — 조사 중 발견한 결함의 한국어 번역 테이블을 구현하고 테스트까지 썼다).
게을러서 어긴 게 아니라 **자기가 뭐 하는 사람인지에 충실했다.**

∴ 금지를 덧붙이는 대신 **자기규정을 갈아끼운다.** ASK 런에는 "change files" 라고
말하는 정체성을 애초에 주지 않는다. 모순이 없으면 뒤에 붙일 반박도 필요 없다 —
그래서 #778 의 별도 지시문 배선은 이 변경으로 삭제된다.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.application._loop_context import system_prompt_for
from backend.workflow.application.agent_briefing import _ASK_SYSTEM_PROMPT, _SYSTEM_PROMPT
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

pytestmark = pytest.mark.asyncio


def _run(classification: str | None) -> ExecutionRun:
    frame: dict[str, str] = {}
    if classification is not None:
        frame["path_classification"] = classification
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": "파일은 하나도 쓰지 마라", "frame": frame},
    )


async def test_ask_run_gets_the_investigator_prompt() -> None:
    assert system_prompt_for(_run("knowledge_only")) == _ASK_SYSTEM_PROMPT


async def test_produce_run_keeps_the_engineer_prompt() -> None:
    assert system_prompt_for(_run("agent_loop")) == _SYSTEM_PROMPT


async def test_run_without_a_frame_keeps_the_engineer_prompt() -> None:
    """No frame → byte-identical to today. A missing classification must never
    silently downgrade a build run into an investigation."""
    assert system_prompt_for(_run(None)) == _SYSTEM_PROMPT
    run = _run("agent_loop")
    run.payload = {"intent_text": "no frame at all"}
    assert system_prompt_for(run) == _SYSTEM_PROMPT


async def test_ask_prompt_never_calls_the_agent_a_file_changer() -> None:
    """The whole point: the contradiction is REMOVED, not argued with.

    `fae09a47` obeyed "change files" over a later "do not change files", so the
    ASK identity must not contain the claim at all.
    """
    lowered = _ASK_SYSTEM_PROMPT.lower()

    assert "change files" not in lowered
    assert "autonomous engineer" not in lowered
    # And it must not carry the write-first workflow that implies producing.
    assert "declare_verification" not in lowered


async def test_ask_prompt_demands_grounding_and_forbids_producing() -> None:
    """Both prod failures must be addressed by the identity itself.

    * `c40c513d` — guessed instead of reading → must demand the files be read.
    * `fae09a47` — implemented what it found → must say answering IS the job.
    """
    lowered = _ASK_SYSTEM_PROMPT.lower()

    assert "file_read" in lowered
    assert "file_edit" in lowered  # named, so the refusal is unambiguous
    assert "do not" in lowered or "never" in lowered


async def test_the_failed_directive_wiring_is_gone() -> None:
    """#778's bolt-on directive is deleted, not left beside its replacement.

    Two prompt sites telling an ASK run different things is how this broke in
    the first place.
    """
    import backend.workflow.application._loop_context as loop_context
    from backend.workflow.application import agent_briefing

    assert not hasattr(agent_briefing, "_ASK_ANSWER_DIRECTIVE")
    assert not hasattr(loop_context, "ask_directive_message")
