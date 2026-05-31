"""Lift B — post-rename import-surface smoke test.

Asserts the mechanical end state of Lift B:

1. ``backend.gateway`` and ``backend.accounts`` no longer exist as importable
   packages.
2. ``backend.router`` now exposes:
   * Lift A facade Protocol + dataclasses (Router, LlmRequest, LlmResult,
     LlmRoutingHints).
   * The union of the previous ``backend.gateway`` public API
     (DispatchError / DispatchRequest / DispatchResult / GatewayDispatcher /
     LlmClient / LlmResponse / ModelAccountNotFound + budget / classifier
     sub-modules).
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
        "ModelAccountRepository",
        "ModelAccountService",
        "ModelAccountUpdate",
        "decrypt_credentials",
        "encrypt_credentials",
    }
    missing = expected - set(dir(mod))
    assert not missing, f"backend.router.accounts missing: {missing}"


def test_router_root_unifies_lift_a_and_old_gateway_exports() -> None:
    router = importlib.import_module("backend.router")
    # From Lift A (facade Protocol + dataclasses).
    lift_a = {"Router", "LlmRequest", "LlmResult", "LlmRoutingHints"}
    # From the former backend.gateway public surface.
    old_gateway = {
        "DispatchError",
        "DispatchRequest",
        "DispatchResult",
        "GatewayDispatcher",
        "LlmClient",
        "LlmResponse",
        "ModelAccountNotFound",
        "budget",
        "classifier",
    }
    available = set(dir(router))
    missing = (lift_a | old_gateway) - available
    assert not missing, f"backend.router missing exports: {missing}"


def test_router_accounts_repository_importable() -> None:
    mod = importlib.import_module("backend.router.accounts.repository")
    assert hasattr(mod, "ModelAccountRepository")


def test_router_dispatch_importable() -> None:
    mod = importlib.import_module("backend.router.dispatch")
    assert hasattr(mod, "GatewayDispatcher")


def test_router_budget_models_importable() -> None:
    """Alembic env relies on this exact path."""
    mod = importlib.import_module("backend.router.budget.models")
    assert hasattr(mod, "GatewayBudgetBase")


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
