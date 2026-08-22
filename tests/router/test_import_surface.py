"""Lift B — post-rename import-surface smoke test.

Asserts the mechanical end state of Lift B:

1. ``backend.gateway`` and ``backend.accounts`` no longer exist as importable
   packages.
2. ``backend.router`` now exposes:
   * Lift A facade Protocol + dataclasses (Router, LlmRequest, LlmResult,
     LlmRoutingHints).
   * The union of the previous ``backend.gateway`` public API
     (DispatchError / DispatchRequest / DispatchResult / GatewayDispatcher /
     LlmClient / LlmResponse / ModelAccountNotFound).
3. ``backend.router.accounts`` now exposes the previous ``backend.accounts``
   public API.

If any of these fail, the rename + import migration is incomplete.
"""

from __future__ import annotations

import importlib

import pytest

# String-built module names so a future blanket sed pass doesn't accidentally
# rewrite the gone-package literals — the whole point of these tests.
_OLD_GATEWAY = "backend." + "gateway"
_OLD_ACCOUNTS = "backend." + "accounts"


def test_old_gateway_package_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_OLD_GATEWAY)


def test_old_top_level_accounts_package_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_OLD_ACCOUNTS)


def test_router_accounts_subpackage_importable() -> None:
    mod = importlib.import_module("backend.router.accounts")
    expected = {
        "DEFAULT_ACCOUNT_LABEL",
        "AccountsBase",
        "CredentialCipher",
        "ModelAccount",
        "ModelAccountCreate",
        "ModelAccountOut",
        # ``ModelAccountRepository`` 는 back-compat shim 의 재수출이었다 —
        # shim 과 함께 2026-08-21 에 빠졌다. 구체 구현은
        # ``router.infrastructure.repositories`` 가, Protocol 은
        # ``router.domain.repositories`` 가 갖는다.
        "ModelAccountService",
        "ModelAccountUpdate",
        "decrypt_credentials",
        "encrypt_credentials",
    }
    missing = expected - set(dir(mod))
    assert not missing, f"backend.router.accounts missing: {missing}"


def test_router_root_exports_the_surface_callers_actually_use() -> None:
    """Lift A 의 facade Protocol + dataclasses(``Router`` / ``LlmRequest`` /
    ``LlmResult`` / ``LlmRoutingHints``)는 여기 있었지만 **소비자가 0** 이라
    2026-08-21 에 지웠다 — ``Router`` 의 "소비자 13곳"은 전부 v8 6-context 모델의
    *Router 컨텍스트*를 가리키는 docstring 산문이었다. 남은 것이 실제 표면이다."""
    router = importlib.import_module("backend.router")
    expected = {
        "DispatchError",
        "LlmClient",
        "LlmResponse",
        "ModelAccountNotFound",
    }
    available = set(dir(router))
    missing = expected - available
    assert not missing, f"backend.router missing exports: {missing}"


def test_router_classifier_subpackage_removed_by_lift_e2() -> None:
    """Lift E2 deletes the classifier (Local/Static/LLM) module entirely."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.router.classifier")


def test_router_dispatch_strategies_removed_by_lift_e2() -> None:
    """Lift E2 deletes the dispatch.strategies seam — the predicate now
    lives at :mod:`backend.router.accounts.predicates`."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.router.dispatch.strategies")


def test_router_accounts_repository_shim_is_gone() -> None:
    """Lift I-Repo 가 남긴 back-compat shim — 스스로 *"the lift's diff stays
    narrow"* 를 위한 임시라고 적었고, 2026-08-21 에 rename 을 완료하며 지웠다.
    구체 구현은 ``router.infrastructure.repositories`` 에, Protocol 은
    ``router.domain.repositories`` 에 있다."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.router.accounts.repository")


def test_router_dispatch_importable() -> None:
    mod = importlib.import_module("backend.router.dispatch")
    # Lift E2 — GatewayDispatcher is gone; only the error surface remains.
    assert hasattr(mod, "DispatchError")
    assert hasattr(mod, "ModelAccountNotFound")


def test_router_routing_subpackage_importable() -> None:
    """The former ``backend.gateway.routing`` (NOT top-level ``backend.routing``)
    moves to ``backend.router.routing``."""
    mod = importlib.import_module("backend.router.routing")
    assert mod is not None


def test_top_level_backend_routing_removed_by_lift_c() -> None:
    """Lift C folds top-level ``backend.routing`` into
    ``backend.router.routing.run_routing`` (asserted positively by the smoke
    test under ``tests/router/routing/run_routing/test_import_surface.py``)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend." + "routing")
