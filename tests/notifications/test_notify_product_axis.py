"""A notification must know — and say — which product it is about.

Founder, 2026-09-11: *"당연히 제품 별로 봇, 이슈, 채널 등을 다 분리할 수 있어야
bsvibe 제품의 철학에 맞아."* PR #922 fixed the INBOUND half (a chat message now
lands on its bound product). This is the outbound half.

Two defects, and the first is upstream of the second:

1. **The notification does not carry a product at all.** ``emit_notification``
   takes ``workspace_id`` and nothing else identifying; the payload carries
   ``run_id`` / ``decision_id`` / ``deliverable_id`` but never ``product_id``.
   So the founder's "작업 완료" card cannot even SAY which product it is about —
   routing was never the first problem, labelling was.
2. **Channel selection has no product filter.** ``resolve_notify_bindings``
   matches on ``workspace_id`` + ``is_active`` only, so every product's
   notifications go to every channel.

**Why (2) needs a fallback rather than a straight filter.** Measured in prod:
``BStockReport`` has a telegram binding; ``BSVibe`` has only a github one, and
github is not a notify channel (``NOTIFY_EVENT_BUILDERS`` = discord /
email-sender / slack / telegram). A naive "only the product's bound channels"
would give BSVibe **zero** channels and silently stop notifications the founder
gets today. So a product with no notify binding keeps the workspace's channels —
which makes this change a no-op on today's prod and meaningful the moment a
second channel exists.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.notifications.bindings import resolve_notify_bindings
from backend.notifications.db import NotificationEventRow
from backend.notifications.emit import emit_notification
from backend.workflow.infrastructure.workers.notify_worker import (
    product_channel_ids as _product_channel_ids,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _workspace(session: AsyncSession) -> uuid.UUID:
    ws = uuid.uuid4()
    session.add(WorkspaceRow(id=ws, name="WS", language="ko"))
    # Flush each FK LEVEL before the rows pointing at it — SQLAlchemy orders
    # inserts per-mapper, and SQLite's lax FK enforcement hides the violation
    # until PostgreSQL runs it.
    await session.flush()
    return ws


async def _product(session: AsyncSession, ws: uuid.UUID, name: str) -> uuid.UUID:
    product = ProductRow(
        id=uuid.uuid4(), workspace_id=ws, name=name, slug=f"p-{uuid.uuid4().hex[:8]}"
    )
    session.add(product)
    await session.flush()
    return product.id


async def _channel(session: AsyncSession, ws: uuid.UUID, connector: str) -> ConnectorAccountRow:
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
    return account


async def _bind(
    session: AsyncSession, ws: uuid.UUID, product_id: uuid.UUID, account: ConnectorAccountRow
) -> None:
    session.add(
        ResourceBindingRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            product_id=product_id,
            connector_account_id=account.id,
            resource_id=uuid.uuid4().hex,
        )
    )
    await session.flush()


# ── 1) the notification carries its product ──────────────────────────────────


async def test_emit_records_the_product(sf) -> None:
    """The producers all hold a run (which holds ``product_id``); nothing asked
    them for it, so the outbox row never knew what it was about."""
    async with sf() as session:
        ws = await _workspace(session)
        product_id = await _product(session, ws, "BStockReport")
        await emit_notification(
            session,
            workspace_id=ws,
            event="shipped",
            dedupe_key=f"shipped:{uuid.uuid4()}",
            payload={"title": "작업 완료", "body": ""},
            producer_id="workflow:verified_deliverable",
            product_id=product_id,
        )
        await session.commit()
        row = (await session.execute(select_latest())).scalar_one()

    assert row.payload["product_id"] == str(product_id)


async def test_emit_without_a_product_stays_valid(sf) -> None:
    """Not every notification is about a product — ``auth_down`` is about the
    workspace. Omitting it must not become an empty string that later reads as
    a product id."""
    async with sf() as session:
        ws = await _workspace(session)
        await emit_notification(
            session,
            workspace_id=ws,
            event="auth_down",
            dedupe_key=f"auth_down:{uuid.uuid4()}",
            payload={"title": "로그인이 안 돼요", "body": ""},
            producer_id="workflow:verified_deliverable",
        )
        await session.commit()
        row = (await session.execute(select_latest())).scalar_one()

    assert "product_id" not in row.payload


async def test_emit_does_not_clobber_the_producers_payload(sf) -> None:
    """The product rides ALONGSIDE what the producer wrote."""
    async with sf() as session:
        ws = await _workspace(session)
        product_id = await _product(session, ws, "BStockReport")
        run_id = str(uuid.uuid4())
        await emit_notification(
            session,
            workspace_id=ws,
            event="shipped",
            dedupe_key=f"shipped:{uuid.uuid4()}",
            payload={"title": "t", "run_id": run_id},
            producer_id="workflow:verified_deliverable",
            product_id=product_id,
        )
        await session.commit()
        row = (await session.execute(select_latest())).scalar_one()

    assert row.payload["run_id"] == run_id
    assert row.payload["product_id"] == str(product_id)


def select_latest():
    from sqlalchemy import select

    return select(NotificationEventRow).order_by(NotificationEventRow.created_at.desc()).limit(1)


# ── 2) channel selection is product-aware, with a fallback ───────────────────


async def test_a_product_with_a_bound_channel_uses_only_that_one(sf) -> None:
    """The founder's stated intent wins over "everything in the workspace"."""
    async with sf() as session:
        ws = await _workspace(session)
        product_id = await _product(session, ws, "BStockReport")
        telegram = await _channel(session, ws, "telegram")
        await _channel(session, ws, "slack")  # present, but not bound to it
        await _bind(session, ws, product_id, telegram)
        await session.commit()

        bindings = await resolve_notify_bindings(
            session,
            workspace_id=ws,
            product_channel_ids=await _product_channel_ids(
                session, workspace_id=ws, product_id=product_id
            ),
        )

    assert [b.connector for b in bindings] == ["telegram"]


async def test_a_product_with_no_notify_binding_keeps_every_channel(sf) -> None:
    """The fallback that makes this safe to land.

    Measured in prod: ``BSVibe`` is bound only to github, and github is NOT a
    notify channel — so a straight filter would leave it with ZERO channels and
    silently stop notifications the founder receives today. Losing an alert is
    strictly worse than one arriving on a channel that also carries another
    product's.
    """
    async with sf() as session:
        ws = await _workspace(session)
        product_id = await _product(session, ws, "BSVibe")
        await _channel(session, ws, "telegram")
        github = await _channel(session, ws, "github")  # not a notify channel
        await _bind(session, ws, product_id, github)
        await session.commit()

        bindings = await resolve_notify_bindings(
            session,
            workspace_id=ws,
            product_channel_ids=await _product_channel_ids(
                session, workspace_id=ws, product_id=product_id
            ),
        )

    assert [b.connector for b in bindings] == ["telegram"]


async def test_a_notification_with_no_product_keeps_every_channel(sf) -> None:
    """``auth_down`` is about the workspace, not a product — it must reach the
    founder everywhere they listen."""
    async with sf() as session:
        ws = await _workspace(session)
        await _channel(session, ws, "telegram")
        await _channel(session, ws, "slack")
        await session.commit()

        bindings = await resolve_notify_bindings(session, workspace_id=ws, product_channel_ids=None)

    assert sorted(b.connector for b in bindings) == ["slack", "telegram"]


async def test_another_products_binding_does_not_narrow_this_one(sf) -> None:
    """Two products, one channel each. Neither may inherit the other's."""
    async with sf() as session:
        ws = await _workspace(session)
        stock = await _product(session, ws, "BStockReport")
        vibe = await _product(session, ws, "BSVibe")
        telegram = await _channel(session, ws, "telegram")
        slack = await _channel(session, ws, "slack")
        await _bind(session, ws, stock, telegram)
        await _bind(session, ws, vibe, slack)
        await session.commit()

        stock_channels = await resolve_notify_bindings(
            session,
            workspace_id=ws,
            product_channel_ids=await _product_channel_ids(
                session, workspace_id=ws, product_id=stock
            ),
        )
        vibe_channels = await resolve_notify_bindings(
            session,
            workspace_id=ws,
            product_channel_ids=await _product_channel_ids(
                session, workspace_id=ws, product_id=vibe
            ),
        )

    assert [b.connector for b in stock_channels] == ["telegram"]
    assert [b.connector for b in vibe_channels] == ["slack"]


async def test_another_workspaces_binding_is_never_consulted(sf) -> None:
    """Workspace isolation on the path that now reads a second table."""
    async with sf() as session:
        ws = await _workspace(session)
        other_ws = await _workspace(session)
        product_id = await _product(session, ws, "BStockReport")
        other_product = await _product(session, other_ws, "Stranger")
        telegram = await _channel(session, ws, "telegram")
        other_telegram = await _channel(session, other_ws, "telegram")
        await _bind(session, other_ws, other_product, other_telegram)
        await session.commit()

        bindings = await resolve_notify_bindings(
            session,
            workspace_id=ws,
            product_channel_ids=await _product_channel_ids(
                session, workspace_id=ws, product_id=product_id
            ),
        )

    # ``ws`` has no binding of its own → fallback → its own channel only.
    assert [b.connector for b in bindings] == ["telegram"]
    assert all(b.account.id == telegram.id for b in bindings)


# ── 3) the wiring: producers pass it, and the card says it ───────────────────


def test_every_run_scoped_producer_passes_the_product() -> None:
    """The hop that makes the rest real — and the one no unit test above sees.

    Each producer holds a run (or, since PR #922, a TriggerEvent) that knows its
    product. A producer that forgets is not an error anywhere: the row simply
    lands product-less and the founder gets the mixed, unlabelled card back.
    So the call sites are asserted directly.

    ``auth_dependency`` and ``daily_brief`` are deliberately absent: one is about
    the workspace's login, the other spans every product.
    """
    import inspect

    from backend.workflow.application import agent_runner, run_persistence
    from backend.workflow.domain import verified_deliverable
    from backend.workflow.infrastructure.workers import intake_worker

    for module in (agent_runner, run_persistence, verified_deliverable, intake_worker):
        source = inspect.getsource(module)
        assert "emit_notification(" in source, module.__name__
        assert "product_id=" in source, (
            f"{module.__name__} emits a run-scoped notification without its product"
        )


def test_the_card_names_its_product() -> None:
    """The founder's actual complaint: every product's cards arrive on one bot
    and none says which product it is about."""
    from backend.notifications.notify_builders import (
        NotificationContent,
        build_telegram_notification,
    )

    content = NotificationContent(
        event="shipped",
        title="작업 완료",
        body="리포트를 만들었어요.",
        language="ko",
        product_name="BStockReport",
    )
    text = build_telegram_notification(content, {"chat_id": "1"}).payload["text"]
    assert "BStockReport" in text


def test_a_product_less_card_is_unchanged() -> None:
    """Negative control: ``auth_down`` has no product and must not grow a stray
    separator or an empty label line."""
    from backend.notifications.notify_builders import (
        NotificationContent,
        build_telegram_notification,
    )

    content = NotificationContent(
        event="auth_down", title="로그인이 안 돼요", body="확인해주세요.", language="ko"
    )
    text = build_telegram_notification(content, {"chat_id": "1"}).payload["text"]
    assert text.startswith("로그인이 안 돼요")


def test_the_worker_joins_the_resolver_to_the_selector() -> None:
    """The wiring — every test above assembles the two halves BY HAND.

    Cutting the worker's call entirely left all 160 notification tests green,
    which is the shape this whole change exists to stop: a per-product axis that
    is fully built and never consulted. ``resource_bindings`` sat in prod for
    months exactly like that.

    Asserted on the source because the seam is a call, not a value: the worker
    must resolve the row's product to channel ids and hand them to the selector.
    """
    import inspect

    from backend.workflow.infrastructure.workers import notify_worker

    source = inspect.getsource(notify_worker.NotifyWorker)
    assert "product_channel_ids=await product_channel_ids(" in source, (
        "the worker does not resolve the row's product before selecting channels"
    )
    assert '"product_id"' in source, "the worker never reads the product off the row"
