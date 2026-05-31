"""Router dispatch strategies — one Protocol, N concrete kinds (Lift D §6.2).

A :class:`ModelAccount` either resolves to a *native LLM call* (cloud HTTP or
local Ollama) or an *external CLI worker* dispatch (claude_code / codex /
opencode). Before Lift D this fork lived as a scattered ``provider ==
"executor"`` predicate at 4 call sites (``workers/run.py`` × 2 + tier_default +
the accounts repository) that each decided independently which path to take.

Lift D collapses the fork to a single :class:`DispatchStrategy` Protocol so the
predicate has ONE owner: :func:`resolve_strategy_kind`. Callers that need to
know "is this an executor account?" ask the resolver, not the column. The
predicate disappears from every site except this one (the strategy resolver) +
the SQL queries that *must* filter the column directly (e.g. the accounts
repository excluding executor rows from the api-llm list endpoint, or the
worker-health probe counting active executor accounts — both legitimate uses
of the column as a discriminator, NOT a duplicated invariant).

Concrete strategies in v1:

* :class:`backend.router.dispatch.strategies.cli_wrapper.CliWrapperStrategy` —
  the executor branch: dispatches to a registered external CLI worker
  (claude_code / codex / opencode) via Redis tasks +
  :class:`~backend.executors.coordinator.ExecutorOrchestrator`.

The native-LLM branch (HTTP / Ollama via
:class:`~backend.router.dispatch.GatewayDispatcher` +
:mod:`backend.router.llm_client`) is the *implicit* default for now — Lift D
introduces the strategy seam alongside the existing dispatcher rather than
re-wiring the LLM path. A future lift (likely Lift I, when the Router facade
gets wired to a concrete impl) will add :class:`HttpStrategy` +
:class:`OllamaStrategy` and route ALL dispatch through ``Router.invoke`` ->
``strategy.execute``.
"""

from __future__ import annotations

from typing import Literal, Protocol

from backend.router.accounts.models import ModelAccount

#: Provider value marking an executor (cloud/opencode CLI) ModelAccount.
#:
#: The SINGLE source of truth for the executor predicate after Lift D. Callers
#: ask :func:`is_executor_account` / :func:`resolve_strategy_kind`; they do not
#: re-compare against this constant inline. The SQL-side queries in
#: :mod:`backend.router.accounts.repository` and the executor-pool health probe
#: in :mod:`backend.workflow.infrastructure.workers.run` use a column predicate (`ModelAccount.provider
#: == "executor"`) — those are SQL filters, not invariant checks, so they reach
#: for the literal directly rather than through this constant.
EXECUTOR_PROVIDER: str = "executor"

#: The kinds :func:`resolve_strategy_kind` returns. ``"cli_wrapper"`` = the
#: executor branch; ``"native_llm"`` = the implicit cloud/Ollama HTTP path
#: (the default; concrete HttpStrategy / OllamaStrategy land in a later lift).
StrategyKind = Literal["cli_wrapper", "native_llm"]


class DispatchStrategy(Protocol):
    """The Lift D dispatch seam — one entry point per account class.

    A future lift wires :class:`~backend.router.facade.Router.invoke` to call
    :meth:`execute` here, so all dispatch (native + executor) flows through one
    surface. Lift D defines the Protocol; concrete strategies on the native LLM
    side will arrive when the facade is wired. The ``CliWrapperStrategy``
    concretely implements the executor branch today.

    The ``execute`` shape stays small so it can later wrap either the
    GatewayDispatcher (cloud HTTP / Ollama) or the ExecutorOrchestrator (CLI
    worker dispatch + verify) without leaking either's internals into the
    Protocol.
    """

    async def execute(self, *args: object, **kwargs: object) -> object: ...


def is_executor_account(account: ModelAccount) -> bool:
    """True when ``account`` routes to the executor CLI-worker branch.

    The SINGLE in-code home of the ``provider == "executor"`` predicate after
    Lift D — every other site that used to repeat the literal now calls this.
    The legitimate SQL-side filters keep their column comparison (see the
    module docstring); only invariant checks route through here.
    """
    return account.provider == EXECUTOR_PROVIDER


def resolve_strategy_kind(account: ModelAccount) -> StrategyKind:
    """Pick the dispatch strategy for ``account``.

    Today: ``"cli_wrapper"`` for executor accounts, ``"native_llm"`` otherwise.
    Callers can use this to decide which compute backend to construct; future
    lifts will let them call ``resolve_strategy(account).execute(...)`` instead.
    """
    return "cli_wrapper" if is_executor_account(account) else "native_llm"


__all__ = [
    "EXECUTOR_PROVIDER",
    "DispatchStrategy",
    "StrategyKind",
    "is_executor_account",
    "resolve_strategy_kind",
]
