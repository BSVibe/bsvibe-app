"""감사 A5 — 툴 표면 seam 을 우회하던 경로의 **잔재**를 지운다.

## 우회 자체는 이미 사라졌다

설계 SoT ``~/Docs/BSVibe_Tool_Surface_Design.md`` §5 의 PR 1·2 는 이미 끝났다:

* **PR 1** (우회 제거) — ``mcp_tool_names_for`` 는 ``execution_target`` 만 받고,
  ``knowledge_only`` 로 분기해 툴 0개로 답하던 경로가 없다. 모든 런이 같은 seam 을 탄다.
* **PR 2** (ASK 브리핑) — ``_loop_context.system_prompt_for`` 가 ASK 면 조사자 정체성을
  **고른다** (#779). 금지를 덧붙이는 게 아니라 정체성을 바꾼다.

남은 것은 그 경로의 **껍데기**다 — 아무도 만들지 않는 오케스트레이터와, 정의만 되고
호출되지 않는 판정 함수.

## 지우는 것

* ``KnowledgeAnswerOrchestrator`` (224 LOC 모듈) — 프로덕션 인스턴스화 **0**.
  참조는 docstring 3곳과 *"우회가 사라졌음"* 을 지키는 가드 테스트뿐이다.
* ``_is_knowledge_only`` — 정의 + 재수출뿐, **호출 0**.

## ⚠️ 함께 지우면 안 되는 것

* **``_ANSWER_SYSTEM_PROMPT``** — 살아 있는 ``DirectAnswerService`` (``/messages/ask``)가
  쓴다. 두 답변 경로가 *"질문이 무엇인가"* 에서 갈라지지 않도록 **공유하던 프롬프트**다.
  유일한 소비자 옆(``direct_answer``)으로 옮긴다.
* **``"knowledge_answer"``** 값 — prod 딜리버러블 **6건**이 이 kind 를 갖고 있다.
  SoT 는 ``verified_deliverable.ANSWER_DELIVERABLE_KIND`` 로 따로 살아 있고,
  사라지는 것은 ``KNOWLEDGE_ANSWER_KIND`` **별칭**뿐이다.
"""

from __future__ import annotations

import importlib

import pytest


def test_the_orchestrator_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.workflow.application.knowledge_orchestrator")


def test_the_uncalled_classifier_is_gone() -> None:
    rt = importlib.import_module("backend.workflow.application.runtime.agent_runtime")
    assert not hasattr(rt, "_is_knowledge_only")
    run = importlib.import_module("backend.workflow.infrastructure.workers.run")
    assert not hasattr(run, "_is_knowledge_only")


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_shared_answer_prompt_survives_next_to_its_consumer() -> None:
    """``/messages/ask`` 가 쓰는 프롬프트다 — 사라지면 인라인 답변이 정체성을 잃는다."""
    da = importlib.import_module("backend.workflow.application.direct_answer")
    assert hasattr(da, "_ANSWER_SYSTEM_PROMPT")
    assert hasattr(da, "DirectAnswerService")


def test_the_stored_deliverable_kind_survives() -> None:
    """prod 딜리버러블 **6건**이 이 값을 갖고 있다 (2026-08-23 실측)."""
    vd = importlib.import_module("backend.workflow.domain.verified_deliverable")
    assert vd.ANSWER_DELIVERABLE_KIND == "knowledge_answer"


def test_every_run_still_takes_the_one_tool_seam() -> None:
    """양성 대조군 — 우회가 없다는 것이 이 PR 의 전제다. 그 seam 이 그대로여야 한다."""
    import inspect

    from backend.workflow.application.tool_registry import mcp_tool_names_for

    params = set(inspect.signature(mcp_tool_names_for).parameters)
    assert params == {"execution_target"}, f"seam 입력이 늘었다: {params}"


def test_the_ask_identity_is_still_selected_not_appended() -> None:
    """양성 대조군 (#779) — ASK 는 정체성을 **고른다**. 금지를 덧붙이지 않는다."""
    from backend.workflow.application._loop_context import system_prompt_for

    assert callable(system_prompt_for)
