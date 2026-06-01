"""ModelAccountRepository Protocol — read/write seam for the Router context.

v8 D44/D45. The :class:`backend.router.accounts.models.ModelAccount` aggregate
holds the per-workspace LLM account roster (native api-llm models + the
``provider='executor'`` rows the worker upsert maintains). Application code
— REST admin handlers, dispatch strategies, the tier-default resolver, the
executor worker register/revoke, the agent_runner Workflow→Router
cross-reference — calls this Protocol instead of issuing raw
``select(ModelAccount)`` queries or instantiating the legacy
:class:`backend.router.accounts.repository.ModelAccountRepository` concrete
class directly.

The method surface mirrors what the legacy class shipped today plus the two
across-workspace queries the run resolver needs
(:meth:`list_active_for_workspace`, used by the tier_default + multi_account
resolvers from a Run that only carries ``workspace_id``). D44 D2/D4 multiple-
active resolution lives in :mod:`backend.router.routing.run_routing` — this
Repository stays a pure persistence seam (the strategy logic doesn't move).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from backend.router.accounts.models import ModelAccount


@runtime_checkable
class ModelAccountRepository(Protocol):
    """Persistence seam for :class:`ModelAccount`.

    Two scoping flavours coexist:

    * Account-scoped — ``(workspace_id, account_id)`` — used by the founder-
      facing api-llm Models surface (admin CRUD).
    * Workspace-scoped — ``workspace_id`` only — used by the run resolver
      (a run only carries ``workspace_id``, and resolution must consider every
      account in the workspace, not just one account's).
    """

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        provider: str,
        label: str,
        litellm_model: str,
        api_base: str | None,
        api_key_encrypted: str | None,
        data_jurisdiction: str,
        extra_params: dict[str, Any],
    ) -> ModelAccount:
        """Stage + flush a new ModelAccount row scoped to
        ``(workspace_id, account_id)``. Caller owns the transaction
        boundary (D45 — no commit here)."""

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> ModelAccount | None:
        """The ModelAccount with this id scoped to ``(workspace_id, account_id)``,
        or ``None``."""

    async def list_for_account(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        only_active: bool = False,
    ) -> Sequence[ModelAccount]:
        """List the api-llm ModelAccounts for ``(workspace_id, account_id)``.

        Executor-pool accounts (``provider='executor'``, Lift 5a) are EXCLUDED
        here — the founder-facing api-llm Models surface shows them separately
        (workers in the PWA). Reachable by id via :meth:`get` and by worker
        via :meth:`list_executor_accounts_for_worker`.
        """

    async def list_active_for_workspace(self, *, workspace_id: uuid.UUID) -> Sequence[ModelAccount]:
        """All ``is_active`` ModelAccounts for ``workspace_id`` (across accounts).

        The run resolver scopes by ``workspace_id`` only — a Run carries no
        ``account_id``, so resolution must look across every account in the
        workspace. Ordered by ``created_at`` ascending (stable, oldest-first)
        so the legacy single-active and the multi-account D4 sort are both
        deterministic on the same fetch.
        """

    async def list_executor_accounts_for_worker(
        self, *, workspace_id: uuid.UUID, worker_id: uuid.UUID
    ) -> Sequence[ModelAccount]:
        """The workspace's executor ModelAccounts bound to ``worker_id``.

        Matches on the ``extra_params.worker_id`` tag the executor upsert
        writes. Used by the worker register/revoke path to find a worker's
        routable models without leaking the JSON-tag filter detail to the
        caller."""

    async def delete(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> bool:
        """Delete the ModelAccount with this id scoped to
        ``(workspace_id, account_id)``. ``True`` if a row was removed,
        ``False`` if no row matched."""

    async def update(self, row: ModelAccount, **fields: Any) -> ModelAccount:
        """Patch ``row`` in place — only non-None ``fields`` are written.
        Returns the same row (mutated). Caller owns the transaction boundary."""


__all__ = ["ModelAccountRepository"]
