"""감사 A15 · D8 — 스스로 임시라고 적은 shim 과, 아무도 안 붙는 seam.

## A15 — 모듈이 자기 수명을 적어뒀다

``backend/router/accounts/repository.py`` docstring:

    *"Back-compat shim … The legacy ``ModelAccountRepository`` symbol is preserved
    as a sub-class of the new ``SqlAlchemyModelAccountRepository`` so the existing
    callers keep working **without a one-shot rename ripple**. …
    **This shim exists so the lift's diff stays narrow.**"*

미뤄둔 rename 을 완료한다. shim 이 더한 것은 ``list_`` 별칭 하나뿐이고, 프로덕션
호출자는 둘(``router.accounts.service`` · ``executors.service``)이다.

⚠️ **이름 충돌 주의** — ``ModelAccountRepository`` 는 **두 곳**에 있다:

* ``router.accounts.repository`` — 이 shim (삭제 대상)
* ``router.domain.repositories.model_account_repository`` — **Protocol** (현역,
  ``api/v1/_router_deps.py`` 의 반환 타입)

``ModelAccountService.list_`` 는 **서비스의 공개 메서드**라 그대로 남는다 — MCP·REST·
테스트가 부른다. 바뀌는 것은 그 안에서 리포지토리를 부르는 이름뿐이다.

## D8 — 형제 셋은 쓰이는데 이 Protocol 만 안 붙는다

``ResourceBindingRepository`` Protocol 은 타입 주석으로도 쓰이지 않는다 —
docstring 언급과 ``__init__`` 재수출뿐이다. 형제(``user`` · ``workspace`` ·
``membership``)는 실제로 의존된다.

⚠️ 같은 모듈의 ``OUTPUT_MODES`` 는 **#804 에서 MCP 가 SoT 로 쓰기 시작했다** —
Protocol 만 지우고 모듈과 상수는 남긴다.
"""

from __future__ import annotations

import importlib

import pytest


def test_the_back_compat_shim_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.router.accounts.repository")


def test_the_router_accounts_package_no_longer_reexports_the_shim() -> None:
    accounts = importlib.import_module("backend.router.accounts")
    assert not hasattr(accounts, "ModelAccountRepository")


def test_the_protocol_of_the_same_name_survives() -> None:
    """양성 대조군 — **이름이 같은 살아 있는 쪽**. 라우터 REST 의존성의 반환 타입이다."""
    domain = importlib.import_module("backend.router.domain.repositories")
    assert hasattr(domain, "ModelAccountRepository")


def test_the_service_public_list_survives() -> None:
    """양성 대조군 — ``ModelAccountService.list_`` 는 MCP·REST 가 부르는 공개 메서드다."""
    from backend.router.accounts.service import ModelAccountService

    assert hasattr(ModelAccountService, "list_")


def test_the_sql_repository_carries_both_methods_the_callers_need() -> None:
    """특성화 — shim 이 감춰주던 이름들이 실제 구현에 있는지."""
    from backend.router.infrastructure.repositories import SqlAlchemyModelAccountRepository

    for name in ("list_for_account", "list_executor_accounts_for_worker", "create", "get"):
        assert hasattr(SqlAlchemyModelAccountRepository, name), name


# ── D8 ────────────────────────────────────────────────────────────────────


def test_the_unattached_seam_is_gone() -> None:
    domain = importlib.import_module("backend.identity.domain.repositories")
    assert not hasattr(domain, "ResourceBindingRepository")


def test_the_output_modes_constant_survives_in_that_module() -> None:
    """양성 대조군 — #804 에서 MCP 가 이 상수를 SoT 로 쓰기 시작했다."""
    mod = importlib.import_module(
        "backend.identity.domain.repositories.resource_binding_repository"
    )
    assert mod.OUTPUT_MODES == frozenset({"safe", "direct"})

    from backend.mcp.tools import bindings_tools

    assert bindings_tools._VALID_OUTPUT_MODES is mod.OUTPUT_MODES  # noqa: SLF001


@pytest.mark.parametrize("name", ["UserRepository", "WorkspaceRepository", "MembershipRepository"])
def test_the_sibling_protocols_survive(name: str) -> None:
    """양성 대조군 — 형제 셋은 실제로 의존된다. 같이 지우면 안 된다."""
    domain = importlib.import_module("backend.identity.domain.repositories")
    assert hasattr(domain, name), name


def test_the_concrete_binding_repository_still_works() -> None:
    """양성 대조군 — 바인딩 CRUD 는 형님이 실제로 쓰는 표면이다."""
    from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository

    assert hasattr(SqlAlchemyResourceBindingRepository, "find_binding")
