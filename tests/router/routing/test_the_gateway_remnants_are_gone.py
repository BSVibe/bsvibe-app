"""BSGateway 라우팅 잔재를 지운다 — 앉을 자리가 빈 폴더였던 코드.

`strategies.py` 가 스스로 실토했다: *"Wired into the dispatch path in Bundle
1.5c **when the LiteLLM hook lands**."* 그 훅이 들어갈 `backend/api/litellm_hook/`
는 `.py` 가 하나도 없는 빈 디렉터리다. 훅은 오지 않았고, 라우팅은 형님 정책
``bsvibe-no-implicit-routing`` 에 따라 ``backend.dispatch`` 로 흐른다.

실측 (2026-08-20):

=======================================================  ===
프로덕션 호출자 (``ModelRegistryService`` 외 5개 심볼)      0
prod ``model_catalog_entries`` 행                        0
prod ``routing_logs`` 행                                 0
=======================================================  ===

유일한 import 는 ``migrations/env.py`` 의 테이블 등록 side-effect 하나였다.

⚠️ **``run_routing/`` 은 현역이다** — 형님이 쓰는 런 라우팅 규칙(``run_routing_rules``)
이 거기 산다. 이 삭제의 대상이 아니고, 아래가 그것을 못박는다.
"""

from __future__ import annotations

import importlib

import pytest

_DEAD_MODULES = (
    "backend.router.routing.registry",
    "backend.router.routing.strategies",
    "backend.router.routing.logs_repository",
    "backend.router.routing.catalog_repository",
    "backend.router.routing.db",
)

_DEAD_NAMES = (
    "ABTestConfig",
    "ABTester",
    "CostOptimizationConfig",
    "CostOptimizer",
    "DbModelRow",
    "GatewayRoutingBase",
    "ModelCatalogDuplicateError",
    "ModelCatalogEntryRow",
    "ModelCatalogReadRepo",
    "ModelCatalogRepository",
    "ModelEntry",
    "ModelRegistryService",
    "RegionConfig",
    "RegionSelector",
    "RoutingLogFeatures",
    "RoutingLogRow",
    "RoutingLogsRepository",
)


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_dead_gateway_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_package_re_exports_none_of_it() -> None:
    """``__init__`` 이 계속 내보내면 삭제가 절반만 된 것이다."""
    pkg = importlib.import_module("backend.router.routing")
    still_there = [name for name in _DEAD_NAMES if hasattr(pkg, name)]
    assert not still_there, f"backend.router.routing 이 아직 내보낸다: {still_there}"


def test_run_routing_is_untouched() -> None:
    """형님이 실제로 쓰는 런 라우팅 규칙 엔진 — 이 삭제가 건드리면 안 된다."""
    db = importlib.import_module("backend.router.routing.run_routing.db")
    assert db.RunRoutingRuleRow.__tablename__ == "run_routing_rules"

    engine = importlib.import_module("backend.router.routing.run_routing.engine")
    for name in ("resolve_route", "RoutingContext", "evaluate_rules"):
        assert hasattr(engine, name), f"run_routing.engine 에서 {name} 이 사라졌다"
