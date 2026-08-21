"""읽는 사람이 없는 읽기 표면과 구현체 0인 훅을 지운다 — 감사 C8 · C9 · D5.

세 건 모두 **코드가 스스로 자백한다**:

* **C8** — "Decisions Resolved" 탭을 위한 읽기 엔드포인트 3개. 그 탭은 없다.
  ``apps/pwa/lib/api/brief.ts:16`` 이 적어뒀다: *"work-stream, resolved HERE
  with context — **there is no separate Decisions tab**"*. PWA·MCP 호출자 0.
* **C9** — ``GET /api/v1/settings`` 는 ``Settings`` 를 변환 없이 미러한다.
  ``"/api/v1/settings"`` 를 부르는 곳이 PWA·백엔드 통틀어 **0**. 미러가 맞는지
  검사하는 테스트가 유일한 소비자였다 — 미러의 존재 이유가 미러 검사였다.
* **D5** — ``ActionDispatchInterceptor`` docstring: *"Lift G registers no
  impls; **the production code path does not call interceptors yet**."*
  ``SettlementSubscriber`` 도 같은 자백을 담고 있다.

``Skill`` 과 ``EventBus`` 는 **남긴다** — 각각 소비자 11곳 · 18곳으로 현역이다.
같은 파일에 있다고 함께 지우면 안 된다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import APIRouter

_DEAD_MODULES = ("backend.api.v1.settings",)  # C9

_DEAD_NAMES = (  # D5
    ("backend.extensions.domain.protocols", "ActionDispatchInterceptor"),
    ("backend.extensions.domain.protocols", "ActionInvocation"),
    ("backend.extensions.domain.protocols", "DispatchDecision"),
    ("backend.extensions.domain.protocols", "SettlementOutcome"),
    ("backend.extensions.domain.protocols", "SettlementSubscriber"),
    ("backend.extensions.domain", "ActionDispatchInterceptor"),
    ("backend.extensions.domain", "SettlementSubscriber"),
)

_DEAD_ROUTES = (  # C8 — (모듈, 경로)
    ("backend.api.v1.safemode.list_get", "/resolved"),
    ("backend.api.v1.checkpoints", "/resolved"),
    ("backend.api.v1.decisions.list_get", "/log"),
)

_LIVE_NAMES = (
    ("backend.extensions.domain.protocols", "Skill"),  # 소비자 11
    ("backend.extensions.domain.protocols", "EventBus"),  # 소비자 18
    ("backend.extensions.domain", "Skill"),
    ("backend.extensions.domain", "EventBus"),
)

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_unread_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize(("module", "name"), _DEAD_NAMES)
def test_the_implementorless_protocol_is_gone(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name), f"{module}.{name} 가 아직 있다"


@pytest.mark.parametrize(("module", "path"), _DEAD_ROUTES)
def test_the_uncalled_route_is_gone(module: str, path: str) -> None:
    """라우터에서 그 경로가 사라졌는지 — 심볼이 아니라 **표면**을 본다."""
    router = importlib.import_module(module).router
    assert path not in {getattr(route, "path", "") for route in router.routes}


def test_the_settings_route_is_no_longer_mounted() -> None:
    """C9 — 라우터 트리에서 ``/settings`` 가 사라진다 (모듈 부재와 별개 축)."""
    from backend.api.v1 import router

    assert not [r for r in router.routes if getattr(r, "path", "").startswith("/settings")]


def test_no_source_still_points_at_the_deleted_surfaces() -> None:
    needles = ("api.v1.settings", "ActionDispatchInterceptor", "SettlementSubscriber")
    offenders = [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(needle in line for needle in needles)
    ]
    assert not offenders, f"삭제된 표면을 아직 가리킨다: {offenders}"


@pytest.mark.parametrize(("module", "name"), _LIVE_NAMES)
def test_the_live_protocols_survive(module: str, name: str) -> None:
    """양성 대조군 — 같은 파일에 있다고 함께 지우면 안 되는 것."""
    assert hasattr(importlib.import_module(module), name), f"{module}.{name} 가 사라졌다"


def test_the_decision_surfaces_the_founder_actually_uses_survive() -> None:
    """양성 대조군 — 삭제하는 것은 '이미 결정된 것'의 읽기 표면뿐이다.
    형님이 실제로 쓰는 **대기 중** 결정 표면과 해소 동작은 그대로여야 한다."""
    from backend.api.v1.checkpoints import router as cp
    from backend.api.v1.decisions.list_get import router as dl
    from backend.api.v1.safemode.list_get import router as sm

    def paths(rt: APIRouter) -> set[str]:
        return {getattr(r, "path", "") for r in rt.routes}

    assert paths(cp) >= {"", "/{checkpoint_id}/resolve"}
    assert paths(sm) >= {"/queue", "/queue/by-run"}
    assert "" in paths(dl)
