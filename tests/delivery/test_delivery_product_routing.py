"""A deliverable goes to ITS product's targets — not the workspace's.

Founder, 2026-09-11: *"당연히 제품 별로 봇, 이슈, 채널 등을 다 분리할 수 있어야
bsvibe 제품의 철학에 맞아."* The inbound half landed in #922, notifications in
#923. This is the last of the three paths.

**github delivery was already product-scoped** (#681/#684/#723) and its docstring
states the principle this file applies to the rest:

    "``None`` is the deliberate safe outcome: the caller … skips github
    delivery, which beats writing to a repo the product does not own."

``_resolve_bindings`` — every OTHER connector — still matched on ``workspace_id``
alone. The explicit-binding gate in front of it stops connectors the founder
never chose, but once a connector is bound for ONE product it qualifies for
EVERY product in the workspace. Prod is exactly one binding away from that:
telegram is an outbound target (``build_telegram_event``, ``@p.outbound``, a
``delivery_config``) and now carries a ``BStockReport`` binding — so a BSVibe
deliverable would be shipped into BStockReport's chat.

It has never fired: ``delivery_events`` is **0** across 251 deliverables (both
bindings are ``output_mode: safe``, so deliverables queue for approval). Latent,
like the verify-slot defect — and worth closing before a second channel or a
flipped output mode makes it real.

**No fallback here, unlike notifications (#923).** There, a product with no bound
channel keeps the workspace's: losing an alert is worse than one arriving on a
shared channel. Delivery inverts that — an artifact written to the wrong place is
outward-facing and hard to take back, so a product with no delivery binding gets
no connector delivery at all.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.workflow.application.delivery.connector_dispatch._resolver import _resolve_bindings

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Plugin:
    """A plugin that declares an outbound — the qualifying condition."""

    outbounds = ({"name": "deliver_message"},)


def _plugins() -> dict[str, Any]:
    return {"telegram": _Plugin(), "slack": _Plugin()}


async def _workspace(session: AsyncSession) -> uuid.UUID:
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="ws", safe_mode=False))
    # Flush each FK LEVEL before the rows pointing at it — SQLite's lax FK
    # enforcement hides the violation until PostgreSQL runs it.
    await session.flush()
    return ws


async def _product(session: AsyncSession, ws: uuid.UUID, name: str) -> uuid.UUID:
    product = ProductRow(id=uuid.uuid4(), workspace_id=ws, name=name, slug=uuid.uuid4().hex[:12])
    session.add(product)
    await session.flush()
    return product.id


async def _target(session: AsyncSession, ws: uuid.UUID, connector: str) -> uuid.UUID:
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ct",
        delivery_config={"chat_id": "1"},
        is_active=True,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _bind(
    session: AsyncSession, ws: uuid.UUID, product_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    session.add(
        ResourceBindingRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id=uuid.uuid4().hex,
        )
    )
    await session.flush()


async def test_a_deliverable_reaches_only_its_products_targets(sf) -> None:
    """Two products, one target each. Neither may ship into the other's."""
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        vibe = await _product(session, ws, "BSVibe")
        telegram = await _target(session, ws, "telegram")
        slack = await _target(session, ws, "slack")
        await _bind(session, ws, stock, telegram)
        await _bind(session, ws, vibe, slack)
        await session.commit()

        for_stock = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=stock
        )
        for_vibe = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=vibe
        )

    assert [b.account.id for b in for_stock] == [telegram]
    assert [b.account.id for b in for_vibe] == [slack]


async def test_a_product_with_no_delivery_binding_ships_nowhere(sf) -> None:
    """NO fallback — the inversion of the notification rule (#923).

    An artifact written to the wrong place is outward-facing and hard to take
    back, so "nothing" beats "somewhere". This mirrors what the github path
    already does: *"None is the deliberate safe outcome … beats writing to a repo
    the product does not own."*
    """
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        unbound = await _product(session, ws, "BSVibe")
        telegram = await _target(session, ws, "telegram")
        await _bind(session, ws, stock, telegram)
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=unbound
        )

    assert resolved == []


async def test_the_live_prod_shape_is_the_one_that_was_wrong(sf) -> None:
    """The defect, stated as the measured prod configuration.

    One bound connector (telegram × BStockReport) previously qualified for every
    product in the workspace, so a BSVibe deliverable shipped into BStockReport's
    chat. The gate in front of the resolver never caught it: it asks "did the
    founder choose this connector at all", not "for THIS product".
    """
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        vibe = await _product(session, ws, "BSVibe")
        telegram = await _target(session, ws, "telegram")
        await _bind(session, ws, stock, telegram)
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=vibe
        )

    assert resolved == [], "a sibling product's deliverable reached this target"


async def test_a_deliverable_with_no_product_ships_nowhere(sf) -> None:
    """A run that never bound a product must not broadcast.

    Before #922 every chat-inbound run was product-less; treating that as "all
    targets" would turn the most ambiguous deliverables into the widest ones.
    """
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        telegram = await _target(session, ws, "telegram")
        await _bind(session, ws, stock, telegram)
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=None
        )

    assert resolved == []


async def test_an_unbound_connector_still_never_qualifies(sf) -> None:
    """Negative control: the ORIGINAL gate must survive.

    Its docstring records why it exists — without it "the founder's telegram
    notification connector was swept in and got a raw duplicate of every
    deliverable". Product scoping is added ON TOP of that, not instead of it.
    """
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        await _target(session, ws, "telegram")  # configured, never bound
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=stock
        )

    assert resolved == []


async def test_another_workspaces_binding_cannot_open_a_target(sf) -> None:
    """Workspace isolation on the path that now filters on a second column."""
    async with sf() as session:
        ws = await _workspace(session)
        other = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        other_product = await _product(session, other, "Stranger")
        telegram = await _target(session, ws, "telegram")
        # The stranger binds OUR account from THEIR workspace — must not count.
        await _bind(session, other, other_product, telegram)
        await session.commit()

        resolved = await _resolve_bindings(
            session, workspace_id=ws, plugins_by_name=_plugins(), product_id=stock
        )

    assert resolved == []


def test_the_dispatcher_passes_the_product_it_already_resolved() -> None:
    """The wiring — every test above calls the resolver directly.

    The dispatch site ALREADY resolves ``product_id`` from the deliverable's run
    (it has to, for the github target since #681). Narrowing that is a one-
    argument change, and forgetting to pass it leaves the resolver
    product-agnostic with every test here still green.
    """
    import inspect

    from backend.workflow.application.delivery import connector_dispatch

    source = inspect.getsource(connector_dispatch)
    call = source.index("_resolve_bindings(")
    window = source[call : call + 400]
    assert "product_id=" in window, "the dispatcher resolves a product and then drops it"
