"""호출자 0인 확장점·잔재 7건을 지운다 — 감사가 아니라 실측 기준으로.

`~/Docs/BSVibe_Unnecessary_Structure_Audit_2026-08-19.md` 의 A7 · B5 · B10 ·
D2 · D4 · D7 · D12. **감사 문장을 근거로 쓰지 않고 전부 현재 코드로 다시 셌다** —
그 과정에서 감사가 세 군데 틀린 것을 찾았다.

=====  ==================================================================
발견   실측이 정정한 것
=====  ==================================================================
A7     ``Router`` 심볼의 "소비자 13곳"은 **전부 docstring 언급**이었다
       (v8 6-context 모델의 *Router 컨텍스트*를 가리키는 산문). 실코드 0.
D7     ``OneShot``/``FixedInterval`` advancer 참조도 **전부 docstring**.
       ``CronScheduleAdvancer`` 와 ``ScheduleAdvancer`` Protocol 은 현역이다.
D2     감사는 ``spec.py`` 를 *"ConnectorCatalog 를 손 복제한 두 번째 출처"* 라
       했지만 **복제가 아니다** — 카탈로그는 *capability* 축, spec 은 *auth* 축.
       진짜 문제는 다른 것이다: spec 은 자기가 *"Backend is the source of
       truth; the PWA mirrors this"* 라고 적어뒀는데 **PWA 는 그것을 읽지
       않는다**. PWA 는 자기 ``connector-fields.ts`` 를 쓴다. 백엔드 소비자도 0.
       실현된 적 없는 SoT 선언이다.
=====  ==================================================================

``A8`` 은 감사가 틀려서 **뺐다** — ``WORK_TOOLS`` 는 ``agent_loop.py:70`` 이
실제로 import 한다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_DEAD_MODULES = (
    "backend.router.facade",  # A7
    "backend.connectors.auth.spec",  # D2
    "backend.common.clamp",  # D12
)

# (모듈, 사라져야 하는 이름)
_DEAD_NAMES = (
    ("backend.knowledge.canonicalization.proposals", "BalancedProposer"),  # B5
    ("backend.knowledge.extraction.worth_remembering", "parse_extraction"),  # B10
    ("backend.knowledge.extraction", "parse_extraction"),  # B10 재수출
    ("backend.storage.product_workspace", "push_product_bundle"),  # D4
    ("backend.schedule.domain.advancer", "OneShotScheduleAdvancer"),  # D7
    ("backend.schedule.domain.advancer", "FixedIntervalScheduleAdvancer"),  # D7
    ("backend.router", "LlmRequest"),  # A7 재수출
    ("backend.router", "LlmResult"),  # A7 재수출
    ("backend.router", "LlmRoutingHints"),  # A7 재수출
    ("backend.router", "Router"),  # A7 재수출
    ("backend.connectors.auth", "CONNECTOR_AUTH"),  # D2 재수출
    ("backend.connectors.auth", "auth_spec_for"),  # D2 재수출
)

# 양성 대조군 — 삭제 전후 모두 살아 있어야 한다.
_LIVE_NAMES = (
    ("backend.workflow.application.tool_registry", "WORK_TOOLS"),  # A8 — 감사가 틀렸다
    ("backend.schedule.domain.advancer", "CronScheduleAdvancer"),  # D7 의 현역 형제
    ("backend.schedule.domain.advancer", "ScheduleAdvancer"),
    ("backend.knowledge.extraction.worth_remembering", "parse_declared_knowledge"),
    ("backend.knowledge.extraction.worth_remembering", "is_inherently_notable"),
    ("backend.connectors.catalog", "get_connector_catalog"),  # INV-1 커넥터 SoT
    ("backend.storage.product_workspace", "publish_product_bundle"),
    ("backend.router", "LlmClient"),
)

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_zero_caller_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize(("module", "name"), _DEAD_NAMES)
def test_the_zero_caller_symbol_is_gone(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name), f"{module}.{name} 가 아직 있다"


def test_no_source_still_points_at_the_deleted_modules() -> None:
    """문자열 import 와 docstring 의 dangling 참조까지 (`backend/` + `tests/`)."""
    needles = (
        "router.facade",
        "connectors.auth.spec",
        "common.clamp",
        # 심볼 단위 삭제는 docstring 에 dangling `:class:`/`:func:` 참조를 남긴다 —
        # 이 PR 에서 7곳이 그렇게 남았고 모듈 needle 로는 안 잡혔다.
        "BalancedProposer",
        "OneShotScheduleAdvancer",
        "FixedIntervalScheduleAdvancer",
        "parse_extraction",
        "push_product_bundle(",
    )
    offenders = [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(needle in line for needle in needles)
    ]
    assert not offenders, f"삭제된 모듈을 아직 가리킨다: {offenders}"


@pytest.mark.parametrize(("module", "name"), _LIVE_NAMES)
def test_the_live_siblings_survive(module: str, name: str) -> None:
    """양성 대조군 — 이름이 비슷하다고 같이 지우면 안 되는 것들."""
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} 가 사라졌다"


def test_the_connector_catalog_still_classifies_every_connector() -> None:
    """양성 대조군 — spec 을 지워도 커넥터 SoT(INV-1 카탈로그)는 그대로다."""
    from backend.connectors.catalog import get_connector_catalog

    assert len(get_connector_catalog()) > 0
