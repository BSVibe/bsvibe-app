"""패턴이 선언 시점에 도달한다 — 관측 모드 (트랙 A-2a).

redesign §5 의 문장이 이 lift 의 사양이다:

> **verify 선언 시점에** BSage retrieval 이 관련된 캡처 패턴을 가져오고,
> **work LLM 이 선언한 것과 함께** verification contract 에 자연스럽게 합류한다.

실측(2026-08-16)으로 확인한 현재 배선에는 **그 지점이 비어 있다**:

| 지점 | 시점 | 받는 쪽 |
|---|---|---|
| B6 seed | 루프 시작 | 에이전트 (의도로만 검색) |
| B3 fold | 검증 시점 | **LLM 판사** — advisory·스킵(E39) |
| **선언 시점** | `declare_verification` | **아무것도 없음** |

E39(#347)가 검색 결과를 판사에게 보내는 것을 advisory 로 내린 판단은 **옳았다**
(run `df66a253`: 잘린 시야 + 단어 쪼가리 기준으로 exit-0 작업을 죽였다). 그러니
패턴은 **판사가 아니라 에이전트**에게, 그것도 계약을 짜는 그 순간에 가야 한다.

**이 lift 는 관측 모드로 나갔다.** seam 만 만들고 응답은 안 바꿨다 — 실제 런에서
무엇이 얼마나 뜨는지 먼저 재기 위해서다. 그 측정이 A-2b(#763)의 필터 규칙을 정했다
(가르침 채널 오탐 0 / 개념·라벨 채널은 무관해도 상시 발생). 설계 SoT 가 정한 순서이고,
오늘 이 순서가 작업 하나를 취소시켰다(백로그 2번).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.workflow.application.tool_registry import assemble_run_tool_registry

pytestmark = pytest.mark.asyncio


class _Retriever:
    """`CanonRetriever` 최소 형태 — 무엇으로 검색됐는지 기록한다."""

    def __init__(self, statements: list[str] | None = None, *, boom: bool = False) -> None:
        self.statements = statements or []
        self.signals: list[str] = []
        self._boom = boom

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.signals.append(signals)
        if self._boom:
            raise RuntimeError("retriever down")
        return list(self.statements)


def _checks() -> dict[str, Any]:
    return {"checks": [{"kind": "command", "command": "uv run pytest tests/test_x.py"}]}


async def test_declaring_consults_the_workspace_patterns(tmp_path: Path) -> None:
    r = _Retriever(["Avoid (prior rejection) — 읽는 쪽까지 배선해라"])
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="판사 시야를 고쳐라"
    )

    await reg.invoke("declare_verification", _checks())

    assert r.signals, "선언 시점에 검색이 일어나야 한다"


async def test_the_signal_carries_the_founder_intent(tmp_path: Path) -> None:
    """B6 는 의도만, B3 는 사후 결과물로 검색한다. 선언 시점은 **의도 + 지금까지
    만진 것**이라 둘보다 낫다."""
    r = _Retriever()
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="판사 시야를 고쳐라"
    )
    (tmp_path / "a.py").write_text("x = 1\n")
    await reg.invoke("file_read", {"path": "a.py"})

    await reg.invoke("declare_verification", _checks())

    signal = r.signals[-1]
    assert "판사 시야를 고쳐라" in signal
    assert "a.py" in signal, "지금까지 만진 파일도 신호에 들어가야 한다"


async def test_what_would_be_injected_is_recorded(tmp_path: Path) -> None:
    """관측 모드의 요점 — 무엇이 주입될지가 남아야 A-2b 를 켜기 전에 잴 수 있다."""
    r = _Retriever(["Avoid (prior rejection) — 읽는 쪽까지 배선해라", "Prior decision — Q: … A: …"])
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="의도"
    )

    await reg.invoke("declare_verification", _checks())

    assert reg.declaration_patterns == [
        "Avoid (prior rejection) — 읽는 쪽까지 배선해라",
        "Prior decision — Q: … A: …",
    ]


async def test_only_founder_teachings_reach_the_response(tmp_path: Path) -> None:
    """A-2a 는 관측 모드라 응답을 안 바꿨다. **A-2b(#763)가 주입을 켰다** —
    다만 형님이 직접 쓴 것만이다.

    개념/라벨 채널은 prod 실측에서 무관한 신호에도 2~3건씩 상시 떴고, 라벨뿐인
    진술은 `df66a253` 를 죽인 그 모양이라 에이전트에게 주지 않는다.
    (필터 계약 전체는 ``test_the_agent_hears_the_founder.py``.)
    """
    r = _Retriever(["Avoid (prior rejection) — 뭔가", "Backend — 개념 노트 본문"])
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="의도"
    )

    out = await reg.invoke("declare_verification", _checks())

    assert "verification contract recorded" in out
    assert "Avoid (prior rejection) — 뭔가" in out
    assert "개념 노트 본문" not in out
    # 관측은 여전히 전부 남는다 — 정밀도를 계속 재야 하므로.
    assert len(reg.declaration_patterns) == 2


async def test_no_retriever_changes_nothing(tmp_path: Path) -> None:
    """빈 워크스페이스 회귀 0 — 검색기가 없으면 예전과 byte-identical."""
    reg = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None)

    out = await reg.invoke("declare_verification", _checks())

    assert "verification contract recorded" in out
    assert reg.declaration_patterns == []


async def test_a_failing_retriever_never_breaks_the_declaration(tmp_path: Path) -> None:
    """검색은 참고일 뿐이다. 그것이 계약 선언을 막으면 본말전도 —
    verify-first 게이트가 쓰기를 막고 있으므로 런 전체가 선다."""
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=_Retriever(boom=True), intent_text="의도"
    )

    out = await reg.invoke("declare_verification", _checks())

    assert "verification contract recorded" in out
    assert reg.declaration_patterns == []


async def test_redeclaring_refreshes_the_patterns(tmp_path: Path) -> None:
    """툴은 `You may call this again to refine the contract` 라고 말한다.
    두 번째 선언은 그 시점의 패턴을 다시 본다 — 오래된 것을 남기지 않는다."""
    r = _Retriever(["첫 번째"])
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="의도"
    )
    await reg.invoke("declare_verification", _checks())

    r.statements = ["두 번째"]
    await reg.invoke("declare_verification", _checks())

    assert reg.declaration_patterns == ["두 번째"]


# ── 두 트랜스포트가 같은 신호를 쓴다 ────────────────────────────────────────


async def test_both_transports_derive_the_same_intent_signal() -> None:
    """executor(MCP)가 프로덕션 경로다. 두 경로의 의도 추출이 갈리면 **한쪽만
    눈이 먼다** — 소스에는 다 적혀 있는데 프로덕션에서만 안 닿는 #752 의 형태.

    MCP 컨텍스트는 ``_loop_context``(``backend.extensions`` 오염)를 import 할 수
    없어 구현이 중복돼 있다. 그래서 **같음을 테스트로** 붙든다.
    """
    import uuid
    from types import SimpleNamespace

    from backend.mcp.tools.work_registry import _intent_of
    from backend.workflow.application._loop_context import _intent_title

    for payload in (
        {"intent_text": "판사 시야를 고쳐라"},
        {"text": "폴백 경로"},
        {"intent_text": "가" * 900},  # 둘 다 512 로 자른다
    ):
        run = SimpleNamespace(id=uuid.uuid4(), payload=payload)
        assert _intent_of(run) == _intent_title(run)  # type: ignore[arg-type]


# ── 관측 모드가 실제로 관측 가능해야 한다 ──────────────────────────────────


async def test_the_observation_survives_the_transport(tmp_path: Path) -> None:
    """⚠️ 이것이 없으면 관측 모드는 관측할 수 없다.

    MCP 트랜스포트는 요청마다 레지스트리를 새로 만든다. 메모리에만 있는 값은
    응답이 끝나면 사라지고 — prod 에서 **잰다는 전제 자체가 성립하지 않는다.**
    (내가 한 시간 전에 거절한 그 결함과 같은 형태: 만들고 기록해놓고 읽는 쪽이 없음.)

    ``export_state`` 는 run 의 ``work_tool_state`` 로 저장되므로 이 값이 거기 실리면
    prod DB 로 셀 수 있다.
    """
    r = _Retriever(["Avoid (prior rejection) — 읽는 쪽까지 배선해라"])
    reg = assemble_run_tool_registry(
        workspace_dir=tmp_path, sandbox=None, retriever=r, intent_text="의도"
    )
    await reg.invoke("declare_verification", _checks())

    state = reg.export_state()
    assert state["declaration_patterns"] == ["Avoid (prior rejection) — 읽는 쪽까지 배선해라"]

    # 그리고 다음 요청의 레지스트리가 그것을 이어받는다.
    fresh = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None)
    fresh.restore_state(state)
    assert fresh.declaration_patterns == ["Avoid (prior rejection) — 읽는 쪽까지 배선해라"]
