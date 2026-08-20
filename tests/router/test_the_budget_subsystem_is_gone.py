"""예산 서브시스템을 지운다 — 구조적으로 절대 발화할 수 없었다.

형님 판정 2026-08-20. 감사 표현으로 *"삭제하거나 완성하거나 둘 중 하나이고 현 상태는
둘 다 아니다"*.

**두 겹으로 막혀 있었다.**

1. **누적이 성립하지 않는다.** ``_build_service`` 가 요청 핸들러 안에서 호출되고
   ``BudgetTracker(InMemoryBudgetStore())`` 를 매번 새로 만든다. 스토어는 dict 한 개
   (자기 docstring: *"A trivial dict-backed store — for tests + dev without Redis"*)라
   ``check_request_cost`` 가 읽는 daily/monthly 는 **항상 0**이고, ``record_actual_cost``
   가 쓴 값은 응답과 함께 GC 된다.
2. **정책 행을 만들 수가 없다.** ``account_budget_policies`` 에 쓰는 유일한 경로인
   ``BudgetPolicyRepository.upsert`` 의 프로덕션 호출자가 **0** — REST 도, MCP 툴도,
   PWA 화면도 없다. prod 실측 **0행**.

∴ ``check_request_cost`` 는 언제나 ``blocked=False`` 를 돌려주고 ``BudgetExceeded`` 는
발화 불가능했다. 유닛테스트는 repository 에 직접 행을 넣고 트래커를 손으로 채워서
green 이었다 — 테스트가 프로덕션이 주지 않는 값을 자기가 넣어주는 그 형태
(``unit-test-supplies-what-production-withholds``).

삭제로 **동작은 변하지 않는다**: 지금도 아무것도 막고 있지 않다.

⚠️ 비용 *보고*는 예산 *강제*와 다르다. 응답의 ``bsvibe.actual_cost_cents`` 는 살아 있는
필드이고 이 삭제의 대상이 아니다 — 아래가 그것을 못박는다.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def test_the_budget_package_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.router" + ".budget")


def test_the_router_package_no_longer_advertises_it() -> None:
    router = importlib.import_module("backend.router")
    assert not hasattr(router, "budget")
    assert "budget" not in router.__all__


def test_the_chat_path_no_longer_speaks_of_budget() -> None:
    """호출부가 남아 있으면 삭제가 절반만 된 것이다 — 그리고 import 가 깨진다."""
    for module_path in ("backend.api.v1.chat", "backend.api.v1.chat_service"):
        source = inspect.getsource(importlib.import_module(module_path))
        for token in ("BudgetExceeded", "BudgetPolicyService", "BudgetTracker", "budget_check"):
            assert token not in source, f"{module_path} 에 {token} 이 남아 있다"


def test_cost_reporting_survives() -> None:
    """비용 보고는 예산 강제와 다른 축이다 — 함께 지우면 관측을 잃는다.

    ``bsvibe.actual_cost_cents`` 는 프록시 응답에 실려 나가는 살아 있는 필드다."""
    chat_service = importlib.import_module("backend.api.v1.chat_service")
    source = inspect.getsource(chat_service)
    assert "_COST_PER_TOKEN_CENTS" in source
    assert '"actual_cost_cents": actual_cost_cents' in source
