"""감사 B6 · D10 — 아무도 안 쓰는 페이사드와, 프로덕션에 등록된 테스트 더블.

## B6 — 페이사드 구현체를 유일한 프로덕션 호출자가 우회한다

``backend/knowledge/application/knowledge.py`` 의 ``SqlAlchemyKnowledge`` /
``build_knowledge`` 는 **프로덕션 소비자가 0**이다 (import 하는 것은 자기 테스트뿐).
유일한 호출 지점인 ``product_bootstrap_runtime`` 은 인라인 ``_BootstrapKnowledge``
스텁을 쓴다 — 결정론적 ``run_id`` · ``proposals_count=0`` · 빈 canon 이라 부트스트랩
고유 의미를 갖는다. 그 의미를 페이사드에 파라미터로 밀어 넣는 것은 결합만 늘린다.

⚠️ 타입(``IngestRequest`` 등)은 ``backend/knowledge/facade.py`` 에 따로 산다 —
이 삭제는 그것을 건드리지 않는다.

## D10 — 테스트 더블이 import 시점에 프로덕션 레지스트리에 등록된다

``providers.py`` 끝에 ``register_provider(StubProvider())`` 가 있다.
*"Seed the stub so the skeleton is exercisable out of the box."*

``provider`` 는 **경로 파라미터**다 — ``get_provider(provider)`` 가 그대로 조회하므로
프로덕션에서 ``/api/v1/connectors/oauth/stub/callback`` 이 테스트 더블에 닿는다.

⚠️ ``StubProvider`` **클래스는 남긴다** — 테스트가 자기 이름으로 등록해서 쓴다
(``StubProvider(name="resolve-stub")`` 등). 지우는 것은 **import 시점 등록**뿐이다.
"""

from __future__ import annotations

import importlib

import pytest


def test_the_unused_knowledge_facade_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.knowledge.application.knowledge")


def test_the_facade_types_survive() -> None:
    """양성 대조군 — 타입은 별도 모듈이고 부트스트랩 스텁이 쓴다."""
    facade = importlib.import_module("backend.knowledge.facade")
    for name in ("IngestRequest", "IngestResult", "CanonRetrievalQuery", "CanonRetrievalResult"):
        assert hasattr(facade, name), name


def test_the_bootstrap_path_still_builds_its_knowledge() -> None:
    """양성 대조군 — 제품 부트스트랩의 ingest 경로는 그대로여야 한다."""
    rt = importlib.import_module("backend.workflow.application.runtime.product_bootstrap_runtime")
    assert "_BootstrapKnowledge" in rt.__dict__ or "_BootstrapKnowledge" in (
        importlib.import_module("inspect").getsource(rt)
    )


def test_the_stub_provider_is_not_registered_at_import_time() -> None:
    """**보안 인접** — 테스트 더블이 프로덕션 레지스트리에 있으면 안 된다."""
    providers = importlib.import_module("backend.connectors.auth.providers")
    assert providers.get_provider("stub") is None, "프로덕션 레지스트리에 stub 이 있다"


def test_the_stub_provider_class_survives_for_tests() -> None:
    """양성 대조군 — 클래스는 남는다. 테스트가 자기 이름으로 등록해서 쓴다."""
    providers = importlib.import_module("backend.connectors.auth.providers")
    assert hasattr(providers, "StubProvider")
    assert hasattr(providers, "register_provider")


def test_a_test_can_still_register_its_own_stub() -> None:
    """특성화 — 테스트가 쓰는 방식은 그대로 동작해야 한다."""
    from backend.connectors.auth.providers import (
        StubProvider,
        get_provider,
        register_provider,
    )

    register_provider(StubProvider(name="guard-probe-stub"))
    assert get_provider("guard-probe-stub") is not None
