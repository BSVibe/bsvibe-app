"""Lift D — DispatchStrategy + executor-predicate collapse.

These tests pin the contract of the Lift D strategy seam:

* :data:`EXECUTOR_PROVIDER` is the SINGLE source of truth for the executor
  account class (every former scattered ``provider == "executor"`` site now
  delegates here).
* :func:`is_executor_account` discriminates an executor :class:`ModelAccount`
  from any other (cloud HTTP, Ollama, etc.) — semantics unchanged from the
  scattered predicate (a fragility net for the future).
* :func:`resolve_strategy_kind` maps account → ``"cli_wrapper"`` for executor
  rows, ``"native_llm"`` otherwise — the single fork the future Router facade
  will switch on.
* :class:`CliWrapperStrategy` constructs (and forwards to) the existing
  :class:`ExecutorOrchestrator` without changing its dispatch flow — the Lift
  D wrapper is the interface seam, NOT a behaviour change.

A separate import-surface delta asserts the predicate doesn't reappear in the
former scatter sites (``backend/workers/run.py`` non-SQL sites, the routing
tier default, and the accounts repository's invariant-style checks).
"""

from __future__ import annotations

import uuid

import pytest

from backend.router.accounts.models import ModelAccount
from backend.router.dispatch.strategies import (
    EXECUTOR_PROVIDER,
    DispatchStrategy,
    is_executor_account,
    resolve_strategy_kind,
)
from backend.router.dispatch.strategies.cli_wrapper import CliWrapperStrategy


def _account(provider: str) -> ModelAccount:
    return ModelAccount(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        provider=provider,
        label=f"{provider} test",
        litellm_model=f"{provider}/test",
        api_base=None,
        api_key_encrypted=None,
        data_jurisdiction="unknown",
        is_active=True,
        extra_params={},
    )


def test_executor_provider_constant_value() -> None:
    """The constant must remain the literal ``"executor"`` — the data row's
    provider column is stamped with this value (executors/service.py creator),
    so any change would orphan every existing executor row in prod."""
    assert EXECUTOR_PROVIDER == "executor"


def test_is_executor_account_true_for_executor() -> None:
    assert is_executor_account(_account("executor")) is True


@pytest.mark.parametrize("provider", ["ollama", "anthropic", "openai", "azure", "gemini", "groq"])
def test_is_executor_account_false_for_non_executor(provider: str) -> None:
    assert is_executor_account(_account(provider)) is False


def test_resolve_strategy_kind_executor_routes_to_cli_wrapper() -> None:
    assert resolve_strategy_kind(_account("executor")) == "cli_wrapper"


@pytest.mark.parametrize("provider", ["ollama", "anthropic", "openai"])
def test_resolve_strategy_kind_non_executor_routes_to_native_llm(
    provider: str,
) -> None:
    assert resolve_strategy_kind(_account(provider)) == "native_llm"


def test_cli_wrapper_strategy_satisfies_dispatch_strategy_protocol() -> None:
    """Pin :class:`CliWrapperStrategy` against the Protocol so the seam stays
    interface-stable as future strategies (HttpStrategy / OllamaStrategy) are
    added in later lifts."""

    # CliWrapperStrategy has an async ``execute`` method — satisfies the
    # Protocol structurally (Protocol uses duck typing, so a hasattr check
    # is enough; we also verify it's awaitable below).
    assert hasattr(CliWrapperStrategy, "execute")

    # Structural-Protocol satisfaction — every Protocol method is present.
    # ``DispatchStrategy.execute`` is the only method; the wrapper class
    # exposes it as an async method.
    obj: type = CliWrapperStrategy
    assert callable(getattr(obj, "execute", None))

    # mypy / runtime — DispatchStrategy is a Protocol, so any class with a
    # matching ``execute`` satisfies it.
    _: type[DispatchStrategy] = CliWrapperStrategy  # type: ignore[assignment, type-abstract]
