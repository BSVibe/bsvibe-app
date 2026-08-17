"""에이전트가 형님의 교정을 듣는다 (트랙 A-2b).

A-2a 가 선언 시점에 조회하는 seam 을 만들고 **관측 모드**로 배포했다. 이 lift 가
그 조회 결과를 에이전트에게 실제로 준다 — 판사가 아니라 **계약을 짜는 에이전트**에게.

**무엇을 줄지는 prod 실측이 정했다** (2026-08-16, 워크스페이스 실물 vault):

| 신호 | 총 | 가르침 | 라벨뿐 | 개념 |
|---|---|---|---|---|
| 판사 시야(겹침) | 6 | **3** | 1 | 2 |
| 알림 문구(무관) | 2 | **0** | 1 | 1 |
| 스케줄(무관) | 2 | **0** | 0 | 2 |
| 검색기(인접) | 3 | **0** | 1 | 2 |

세 가지가 드러났다:

1. **가르침 채널은 오탐이 0이다.** 겹치는 신호에서만 뜨고 무관한 세 신호에서 전부 0.
2. **잡음은 전부 개념/라벨 채널에서 온다.** 무관해도 2~3건씩 상시 나온다 — 신호 문제가
   아니라 채널 성격이다.
3. 라벨뿐인 진술(``Verification``, ``Llm-backend``)은 **`df66a253` 를 죽인 그 모양**
   그대로다 — 단어 쪼가리 기준으로 판사가 exit-0 작업을 죽였던 그 사건.

∴ **형님이 직접 쓴 것만 준다.** 개념 앵커는 §6 이 말한 *결정론적 표면화* 의 재료이지
에이전트가 읽을 참고문이 아니다. ratchet 이 나르려는 것은 **형님의 교정**이다.

주입 대가도 계산돼 있다: 겹치면 3줄, 무관하면 **0줄**.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.workflow.application.tool_registry import assemble_run_tool_registry

pytestmark = pytest.mark.asyncio

_TEACHING = "Avoid (prior rejection) — 새 신호를 만들었으면 읽는 쪽까지 배선해라"
_DECISION = "Prior decision — Q: 검토가 필요해요 A: 명세는 끝났다. 더 고치지 마라."
_CONCEPT = "Backend — Add a human-readable byte-size formatter to the backend. Add a small list"
_LABEL = "Verification"


class _Retriever:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        return list(self.statements)


def _checks() -> dict[str, Any]:
    return {"checks": [{"kind": "command", "command": "uv run pytest tests/test_x.py"}]}


def _reg(tmp_path: Path, statements: list[str]):
    return assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=_Retriever(statements), intent_text="의도"
    )


async def test_the_founders_correction_reaches_the_agent(tmp_path: Path) -> None:
    """이 트랙 전체가 여기로 온다 — 형님의 거절이 다음 런의 에이전트에게 닿는다."""
    out = await _reg(tmp_path, [_TEACHING]).invoke("declare_verification", _checks())

    assert "읽는 쪽까지 배선해라" in out


async def test_a_prior_decision_reaches_the_agent(tmp_path: Path) -> None:
    out = await _reg(tmp_path, [_DECISION]).invoke("declare_verification", _checks())

    assert "명세는 끝났다" in out


async def test_concept_anchors_are_not_injected(tmp_path: Path) -> None:
    """개념 노트는 무관한 신호에서도 상시 뜬다(실측). 참고문이 아니라 앵커다."""
    out = await _reg(tmp_path, [_CONCEPT]).invoke("declare_verification", _checks())

    assert "byte-size formatter" not in out


async def test_bare_labels_are_not_injected(tmp_path: Path) -> None:
    """``Verification`` 같은 라벨뿐인 진술은 `df66a253` 를 죽인 바로 그 모양이다."""
    out = await _reg(tmp_path, [_LABEL, "Llm-backend"]).invoke("declare_verification", _checks())

    assert "Verification" not in out.replace("verification contract recorded", "")
    assert "Llm-backend" not in out


async def test_only_the_teachings_survive_a_mixed_result(tmp_path: Path) -> None:
    """실측된 그 6건 모양 — 가르침 3 + 잡음 3 이 들어오면 3줄만 나간다."""
    out = await _reg(tmp_path, [_CONCEPT, _TEACHING, _LABEL, _DECISION]).invoke(
        "declare_verification", _checks()
    )

    assert "읽는 쪽까지 배선해라" in out
    assert "명세는 끝났다" in out
    assert "byte-size formatter" not in out


async def test_an_unrelated_run_sees_no_change(tmp_path: Path) -> None:
    """무관한 작업에는 **아무것도 안 붙는다** — 실측에서 가르침 0건이던 그 경우.
    응답이 예전과 byte-identical 이어야 에이전트의 주의를 낭비하지 않는다."""
    out = await _reg(tmp_path, [_CONCEPT, _LABEL]).invoke("declare_verification", _checks())

    assert out == (
        "verification contract recorded: 1 command check(s), 0 judge check(s). "
        "Now write the tests, then implement."
    )


async def test_the_agent_is_told_these_are_optional(tmp_path: Path) -> None:
    """게이팅이 아니다(§11: 별도 집행 메커니즘 아님). 계약의 주인은 에이전트이고
    이건 참고다 — 그렇게 말해줘야 무관한 줄을 무시할 수 있다."""
    out = await _reg(tmp_path, [_TEACHING]).invoke("declare_verification", _checks())

    assert "refine" in out.lower() or "다듬" in out


async def test_no_retriever_is_byte_identical(tmp_path: Path) -> None:
    """빈 워크스페이스 회귀 0."""
    reg = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None)

    out = await reg.invoke("declare_verification", _checks())

    assert out == (
        "verification contract recorded: 1 command check(s), 0 judge check(s). "
        "Now write the tests, then implement."
    )


async def test_the_observation_still_records_everything(tmp_path: Path) -> None:
    """주입은 걸러도 **관측은 전부** 남는다 — 정밀도를 계속 재려면 잡음도 보여야 한다."""
    reg = _reg(tmp_path, [_CONCEPT, _TEACHING, _LABEL])
    await reg.invoke("declare_verification", _checks())

    assert reg.declaration_patterns == [_CONCEPT, _TEACHING, _LABEL]
