"""Personal-Account provisioning (the billing/partition account axis).

``ensure_personal_account`` is the one primitive both the login bootstrap
(§10.1) and the ``GET /api/v1/account`` discovery endpoint call: get-or-create
the workspace's personal :class:`Account`. Idempotent and create-on-read safe,
so a returning founder is backfilled on next login and a fresh fetch never
races a missing row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.accounts.account_models import Account
from backend.accounts.service import DEFAULT_ACCOUNT_LABEL


async def ensure_personal_account(session: AsyncSession, *, workspace_id: uuid.UUID) -> Account:
    """Return the workspace's personal account, creating it if absent.

    Resolution is earliest-created-wins (``created_at`` then ``id`` for a
    stable tiebreak), leaving room for future multi-account workspaces without
    a unique constraint on ``workspace_id``. The new row is flushed (so it has
    a usable id) but NOT committed — the caller owns the transaction boundary,
    which lets bootstrap fold it into the same commit as the workspace +
    membership.
    """
    stmt = (
        select(Account)
        .where(Account.workspace_id == workspace_id)
        .order_by(Account.created_at.asc(), Account.id.asc())
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    account = Account(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        label=DEFAULT_ACCOUNT_LABEL,
    )
    session.add(account)
    await session.flush()
    return account
