"""The binding's ``selection`` is where THIS product's delivery target lives (#1003).

Founder, 2026-09-18: BStockReport mapping to a 1:1 chat is intended, but *other
users may want a group chat, so it has to be supported*. Measuring that split the
question into three axes, and this file is the outbound one.

**Delivery destination came only from the ACCOUNT.** ``build_telegram_event`` reads
``delivery_config['chat_id']`` — a per-``connector_account`` value — so every product
bound to one account shipped into the same room. Splitting them meant creating a
second connector account (a second bot token) purely to hold a second ``chat_id``.

⭐ **The per-binding slot already existed, already populated, and nobody read it.**
Measured against prod 2026-09-22, all three live bindings:

    binding.selection                                    account.delivery_config
    {"chat_id": "8242700007"}                            {"chat_id": "8242700007"}
    {"chat_id": "8242700007"}                            {"chat_id": "8242700007"}
    {"repo": "BSVibe/bsvibe-app", "base_branch": "main"} same

``_resolve_bindings`` selected ``ResourceBindingRow.connector_account_id`` and nothing
else, so ``selection`` never reached the dispatch loop. Because every live row already
agrees with its account, **honouring it is a provable no-op on today's data** — which is
what makes this safe to ship ahead of anyone needing it. ``test_todays_prod_shape_is_a_no_op``
below is that proof, stated as a test so it cannot quietly stop being true.

**Precedence is account-then-binding**, the narrower one last: a binding says something
about ONE product × account pair, the account speaks for all of them.

⚠️ **Two consumers, one merge.** The dispatch loop feeds the config to the event builder
AND to the plugin context. Merging at one and not the other would route the event to the
group while the plugin still ran against the 1:1 config — so the merged value is computed
once and both read that single name. ``test_the_plugin_context_sees_the_same_config``
is the call site that a one-line fix would have missed.

**github is deliberately NOT in scope.** ``deliver_github`` takes a :class:`GithubBinding`,
resolved by matching the PRODUCT's own ``repo_url`` against the account (#681/#684/#723) —
it never consults ``resource_bindings`` at all. That path is already per-product by a
different mechanism; reaching into it here would fight an intentional design.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.workflow.application.delivery.connector_dispatch._destination import (
    effective_delivery_config,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import _resolve_bindings

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Plugin:
    outbounds = ({"name": "deliver_message"},)


def _plugins() -> dict[str, Any]:
    return {"telegram": _Plugin()}


async def _workspace(session: AsyncSession) -> uuid.UUID:
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="ws", safe_mode=False))
    await session.flush()
    return ws


async def _product(session: AsyncSession, ws: uuid.UUID, name: str) -> uuid.UUID:
    product = ProductRow(id=uuid.uuid4(), workspace_id=ws, name=name, slug=uuid.uuid4().hex[:12])
    session.add(product)
    await session.flush()
    return product.id


async def _target(
    session: AsyncSession, ws: uuid.UUID, delivery_config: dict[str, Any]
) -> uuid.UUID:
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector="telegram",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ct",
        delivery_config=delivery_config,
        is_active=True,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _bind(
    session: AsyncSession,
    ws: uuid.UUID,
    product_id: uuid.UUID,
    account_id: uuid.UUID,
    selection: dict[str, Any] | None = None,
) -> None:
    session.add(
        ResourceBindingRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id=uuid.uuid4().hex,
            selection=selection or {},
        )
    )
    await session.flush()


async def _resolve_one(sf, *, selection: dict[str, Any], account_config: dict[str, Any]):
    """One product, one account, one binding — return the resolved binding."""
    async with sf() as session:
        ws = await _workspace(session)
        product = await _product(session, ws, "P")
        account = await _target(session, ws, account_config)
        await _bind(session, ws, product, account, selection)
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=product
        )
    assert len(resolved) == 1
    return resolved[0]


async def test_the_binding_selection_reaches_the_dispatch_loop(sf) -> None:
    """The slot was populated all along; the resolver simply never loaded it."""
    binding = await _resolve_one(
        sf, selection={"chat_id": "-100999"}, account_config={"chat_id": "555"}
    )

    assert binding.selection == {"chat_id": "-100999"}


async def test_the_binding_destination_wins_over_the_accounts(sf) -> None:
    """Precedence: the narrower statement last. Without this, splitting two products
    onto two chats needs a second connector account (a second bot token) whose only
    job is to hold a second ``chat_id``."""
    binding = await _resolve_one(
        sf, selection={"chat_id": "-100999"}, account_config={"chat_id": "555"}
    )

    assert effective_delivery_config(binding)["chat_id"] == "-100999"


async def test_an_empty_selection_leaves_the_account_config_alone(sf) -> None:
    """``selection`` defaults to ``{}`` — the overwhelming majority of rows. An
    override slot whose empty value changed the destination would be a defect that
    shipped to everyone at once."""
    binding = await _resolve_one(sf, selection={}, account_config={"chat_id": "555"})

    assert effective_delivery_config(binding) == {"chat_id": "555"}


async def test_todays_prod_shape_is_a_no_op(sf) -> None:
    """The measured prod rows, stated as a test.

    On 2026-09-22 every live binding's ``selection`` equalled its account's
    ``delivery_config``, so turning this on could not move a single delivery. That
    is the whole safety argument for shipping it before anyone needs it — and a
    claim about live data belongs somewhere that fails when it stops holding, not
    only in a PR description.
    """
    same = {"chat_id": "8242700007"}
    binding = await _resolve_one(sf, selection=dict(same), account_config=dict(same))

    assert effective_delivery_config(binding) == same


async def test_selection_keys_the_builder_ignores_do_not_disturb_the_destination(sf) -> None:
    """``selection`` is also the INBOUND routing-hint slot (``intake.py`` reads
    ``artifact_type`` from it and copies the dict onto the payload). A binding that
    carries both must still deliver to the right room."""
    binding = await _resolve_one(
        sf,
        selection={"chat_id": "-100999", "artifact_type": "code"},
        account_config={"chat_id": "555"},
    )

    config = effective_delivery_config(binding)
    assert config["chat_id"] == "-100999"
    assert config["artifact_type"] == "code"


async def test_two_bindings_that_disagree_deliver_to_neither_guess(sf) -> None:
    """``resource_bindings`` has NO uniqueness on (product, account) — two rows for
    one pair are legal, and they can name different rooms.

    Picking one would make the destination depend on row order. Delivery is
    outward-facing and hard to take back, so an ambiguous override is DROPPED and the
    account's own configured target stands — the documented behaviour, not a guess.
    """
    async with sf() as session:
        ws = await _workspace(session)
        product = await _product(session, ws, "P")
        account = await _target(session, ws, {"chat_id": "555"})
        await _bind(session, ws, product, account, {"chat_id": "-100111"})
        await _bind(session, ws, product, account, {"chat_id": "-100222"})
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=product
        )

    assert len(resolved) == 1
    assert effective_delivery_config(resolved[0]) == {"chat_id": "555"}


async def test_two_bindings_that_agree_still_override(sf) -> None:
    """The control for the rule above: duplication is not by itself ambiguity. If it
    were, the guard would be refusing whenever a pair simply has two rows.
    """
    async with sf() as session:
        ws = await _workspace(session)
        product = await _product(session, ws, "P")
        account = await _target(session, ws, {"chat_id": "555"})
        await _bind(session, ws, product, account, {"chat_id": "-100999"})
        await _bind(session, ws, product, account, {"chat_id": "-100999"})
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=product
        )

    assert effective_delivery_config(resolved[0])["chat_id"] == "-100999"


async def test_a_workspace_wide_resolution_has_no_binding_to_read(sf) -> None:
    """``product_id=None`` resolves nothing today (the explicit-binding gate), so
    there is no binding whose selection could apply. Pinned so a future change to
    that gate cannot silently start applying one product's room to a workspace-wide
    delivery."""
    async with sf() as session:
        ws = await _workspace(session)
        product = await _product(session, ws, "P")
        account = await _target(session, ws, {"chat_id": "555"})
        await _bind(session, ws, product, account, {"chat_id": "-100999"})
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=None
        )

    assert resolved == []
