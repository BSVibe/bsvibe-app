"""Back-compat shim — the canonical ModelAccount Repository now lives at
:mod:`backend.router.infrastructure.repositories.model_account_repository_sql`
(Lift I-Repo-Router).

The legacy ``ModelAccountRepository`` symbol is preserved as a sub-class of
the new ``SqlAlchemyModelAccountRepository`` so the existing callers
(:class:`backend.router.accounts.service.ModelAccountService` and the
executor worker register/revoke path in :mod:`backend.executors.service`)
keep working without a one-shot rename ripple. The legacy class exposed
``list_(...)`` instead of the cleaner :meth:`list_for_account`; the alias
below keeps the method available.

New code should import the Protocol from
:mod:`backend.router.domain.repositories` and the SQL impl from
:mod:`backend.router.infrastructure.repositories`. This shim exists so
the lift's diff stays narrow.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from backend.router.infrastructure.repositories.model_account_repository_sql import (
    SqlAlchemyModelAccountRepository,
)

if TYPE_CHECKING:
    import uuid

    from backend.router.accounts.models import ModelAccount


class ModelAccountRepository(SqlAlchemyModelAccountRepository):
    """Legacy alias — keeps the pre-Lift-I name available for in-tree callers.

    Adds a thin ``list_`` alias around the renamed :meth:`list_for_account`
    so the legacy method name (which is the same query) keeps working."""

    async def list_(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        only_active: bool = False,
    ) -> Sequence[ModelAccount]:
        return await self.list_for_account(
            workspace_id=workspace_id,
            account_id=account_id,
            only_active=only_active,
        )


__all__ = ["ModelAccountRepository"]
