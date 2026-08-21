"""감사 C3 · C12 — MCP 가 REST 를 손으로 미러한 것을 없앤다.

이것은 **삭제가 아니라 리팩터**다. 부재 가드 대신 두 가지를 쓴다:

1. **구조 가드** (RED→GREEN) — 같은 것의 정의가 **하나뿐**인지
2. **특성화 테스트** (계속 GREEN) — 동작이 그대로인지

## C3 — 이미 REST 를 import 하면서 스키마를 복제했다

``mcp/tools/run_routing_rules_tools.py`` 는 ``backend.api.v1.run_routing`` 에서
**9개 심볼**(``apply_proposals`` · ``compile_for_workspace`` · 비공개
``_validate_caller_id`` 까지)을 이미 가져온다. 그런데 ``ConditionPayload`` 만은
**바이트 동일하게 다시 선언**했다 (본문 24줄 완전 일치, 실측).

import-linter 에 ``backend.mcp.tools.run_routing_rules_tools ->
backend.api.v1.run_routing`` 예외가 **이미 있다** — 심볼 하나를 더 가져오는 데
새 예외가 필요하지 않다. 미러를 지우는 쪽이 계약 표면을 넓히지 않는다.

## C12 — 레지스트리 위에 손으로 센 숫자를 얹었다

``register_all_tools`` docstring 이 표면별 툴 수를 요약했는데, 실측하면:

======================  ========  ========
표면                      docstring   실제
======================  ========  ========
connectors                      5        13
safe-mode                       3         7
knowledge                       5         8
run-routing-rules               3         6
graph / products / runs /
deliverables / workers     (없음)   5/6/4/2
======================  ========  ========

총 **88개**인데 요약은 ~55개를 말한다. 레지스트리가 SoT 인데 그 위에 두 번째
표현을 얹었고, 예상대로 드리프트했다. **숫자를 고치는 게 아니라 없앤다.**
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.mcp.api import ToolRegistry
from backend.mcp.tools import register_all_tools

_ROOT = Path(__file__).resolve().parents[2]


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


# ── C3 — 구조 가드 ────────────────────────────────────────────────────────


def test_condition_payload_is_declared_once() -> None:
    """``ConditionPayload`` 정의가 트리 전체에 **하나**여야 한다."""
    sites = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in (_ROOT / "backend").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("class ConditionPayload")
    ]
    assert len(sites) == 1, f"ConditionPayload 가 여러 곳에 선언돼 있다: {sites}"


def test_the_mcp_tool_uses_the_rest_condition_schema() -> None:
    """특성화 — MCP 와 REST 가 **같은 클래스 객체**를 쓴다 (미러가 아니라 공유)."""
    from backend.api.v1.run_routing import ConditionPayload as RestCondition
    from backend.mcp.tools import run_routing_rules_tools as mcp

    assert mcp.ConditionPayload is RestCondition


def test_the_condition_validator_still_rejects_an_unknown_operator() -> None:
    """특성화 — 통합 후에도 검증 동작이 그대로여야 한다."""
    import pytest
    from pydantic import ValidationError

    from backend.api.v1.run_routing import ConditionPayload

    with pytest.raises(ValidationError):
        ConditionPayload(field="intent", operator="definitely-not-an-operator", value="x")


# ── C12 — 구조 가드 ───────────────────────────────────────────────────────


def test_the_tool_registry_docstring_carries_no_hand_counted_numbers() -> None:
    """레지스트리가 SoT 다 — docstring 이 표면별 개수를 세면 반드시 드리프트한다.

    실측(2026-08-21): connectors 5→13 · safe-mode 3→7 · knowledge 5→8 ·
    run-routing 3→6, 그리고 graph/products/runs/deliverables/workers 는 누락."""
    from backend.mcp.tools import register_all_tools as fn

    doc = fn.__doc__ or ""
    counted = re.findall(r"\(\s*\d+\s*(?:—[^)]*)?\)", doc)
    assert not counted, f"docstring 이 손으로 센 숫자를 담고 있다: {counted}"


def test_every_registered_tool_is_reachable_from_the_registry() -> None:
    """특성화 — 통합이 툴을 잃지 않았는지. 실측 기준선 88개 이상."""
    reg = _registry()
    names = set(reg._tools)  # noqa: SLF001 — 레지스트리 내부가 곧 SoT 다
    assert len(names) >= 88, f"툴 수가 줄었다: {len(names)}"
    for expected in (
        "bsvibe_run_routing_rules_create",
        "bsvibe_safe_mode_approve",
        "bsvibe_connectors_list",
        "bsvibe_graph_search",
    ):
        assert expected in names, f"툴이 사라졌다: {expected}"
