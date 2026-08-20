"""소비자 0인 "추출된 라이브러리"를 지운다 — 통합으로 다른 제품이 사라졌다.

형님 판정 2026-08-20 (감사 D1). ``backend/shared/fastapi/`` 는 여러 제품이 공유할
FastAPI 기본기(CORS · health · 미들웨어 · 설정)로 추출됐는데, 통합 단일제품
전환으로 그 "여러 제품"이 사라졌다. 남은 하나는 그것을 쓰지 않는다 —
``backend/api/health.py`` 가 자백한다:

    *"The lifted ``backend.shared.fastapi.health`` provides a generic primitive
    (``make_health_router``). Phase 0 keeps this thin product route separate…"*

**존재를 알면서 안 쓰고 자체 라우터를 쓴다.**

⚠️ ``shared/core`` 는 **절반만** 죽었다 — 감사보다 정밀하게 세었다:

===================================  ==========  ========
심볼                                  외부 소비자   판정
===================================  ==========  ========
``redact_url_password``                       4   **유지**
``configure_logging``                         3   **유지**
``csv_list_field`` / ``parse_csv_list``       3   **유지**
``HttpClientBase``                            0   삭제
``BsvibeSettings``                            0   삭제
``BsvibeError`` 외 예외 3종                     0   삭제
``types.py`` 별칭 5종                          0   삭제
===================================  ==========  ========

``redact_headers`` 는 외부 소비자가 0인데 ``HttpClientBase`` 안에서만 쓰였으므로
그것과 함께 사라진다 — 이 삭제가 만든 결과이지 별도 판단이 아니다.
"""

from __future__ import annotations

import importlib

import pytest

_DEAD_MODULES = (
    "backend.shared.fastapi",
    "backend.shared.fastapi.cors",
    "backend.shared.fastapi.health",
    "backend.shared.fastapi.middleware",
    "backend.shared.fastapi.settings",
    "backend.shared.core.exceptions",
    "backend.shared.core.types",
)

_DEAD_NAMES = (
    "BsvibeError",
    "BsvibeSettings",
    "ConfigurationError",
    "HttpClientBase",
    "NotFoundError",
    "ValidationError",
    "redact_headers",
)

_LIVE_NAMES = ("configure_logging", "csv_list_field", "parse_csv_list", "redact_url_password")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_consumerless_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_core_package_no_longer_exports_the_dead_half() -> None:
    core = importlib.import_module("backend.shared.core")
    still_there = [name for name in _DEAD_NAMES if hasattr(core, name)]
    assert not still_there, f"backend.shared.core 가 아직 내보낸다: {still_there}"


def test_the_live_half_survives() -> None:
    """양성 대조군 — 이 넷은 프로덕션이 실제로 부른다.

    ``redact_url_password`` 가 사라지면 DB URL 의 비밀번호가 로그로 샌다."""
    core = importlib.import_module("backend.shared.core")
    missing = [name for name in _LIVE_NAMES if not hasattr(core, name)]
    assert not missing, f"살아 있는 심볼이 사라졌다: {missing}"


def test_the_product_still_serves_its_own_health_route() -> None:
    """제품이 자기 라우터를 쓴다 — 지운 것은 아무도 안 쓰던 generic primitive 다."""
    health = importlib.import_module("backend.api.health")
    assert hasattr(health, "router")
