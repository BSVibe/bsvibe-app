"""Safe-Mode approve path delivers through the connector adapter — §10.5 / §11.1.

The Direct path (Safe Mode OFF) already delivers a verified Deliverable OUT
through :class:`~backend.delivery.connector_dispatch.ConnectorDeliveryAdapter`
(proved by ``test_connector_deliver_e2e``). But Safe Mode defaults ON, so MOST
deliveries flow through the founder-approval gate
(``POST /api/v1/safemode/{id}/approve``). This proves that the approve path uses
the SAME ``ConnectorDeliveryAdapter`` — an approved delivery shapes + delivers
the connector event correctly:

    (verified run) → Deliverable + DeliveryEventRow
      → DeliveryWorker.drain_once   → SafeModeQueueItem (pending), NO dispatch
      → POST /api/v1/safemode/{id}/approve
          → ConnectorDeliveryAdapter resolves the notion binding
          → shapes the event (parent_page_id from config, title/body from the
            deliverable) and POSTs it to Notion (respx-mocked)
          → item APPROVED

Plus the no-binding branch: approve a workspace with no connector binding →
approved, NO external HTTP call, no error (mirrors the Direct path).

The approve route resolves its dispatcher via the overridable
``get_delivery_dispatcher`` dependency. In production this builds a connector
adapter over the process session factory + settings cipher; here we override it
with a connector adapter built against the test session factory + test cipher —
so the SAME real adapter / shaping is exercised, not a stub sink.

In-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL`` is set
(mirrors the other glue tests).
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.accounts.crypto import CredentialCipher
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.safemode import get_delivery_dispatcher
from backend.config import get_settings
from backend.connectors.db import ConnectorAccountRow
from backend.data import Base
from backend.delivery.connector_dispatch import (
    ConnectorDeliveryAdapter,
    build_connector_delivery_adapter,
)
from backend.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.plugins.implementations.notion import plugin as notion_module
from backend.plugins.loader import PluginLoader
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workspaces.db import WorkspaceRow

from .._support import fake_current_user

NOTION_API = "https://api.notion.test"

# Deterministic 32-byte AES key for CredentialCipher in tests (matches the
# connector glue tests' pattern).
TEST_KEY = b"0123456789abcdef0123456789abcdef"

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)

pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def sf():
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    if use_pg:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def _plugins() -> list[object]:
    impl_dir = Path(notion_module.__file__).resolve().parents[1]
    registry = await PluginLoader(impl_dir).load_all()
    return list(registry.values())


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession],
    founder_id: uuid.UUID,
    workspace_id: uuid.UUID,
    cipher: CredentialCipher,
):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    # The approve route resolves the delivery dispatcher via this dependency. We
    # override it with the SAME real ConnectorDeliveryAdapter (built against the
    # test session factory + test cipher + the loaded plugins) the production
    # dependency builds — so the approve path exercises real connector shaping.
    plugins = await _plugins()

    def _dispatcher():
        return build_connector_delivery_adapter(session_factory=sf, plugins=plugins, cipher=cipher)

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_delivery_dispatcher] = _dispatcher

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_verified_deliverable(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    """Seed a Safe-Mode ON workspace + a verified Deliverable + its DeliveryEvent."""
    session.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=True))
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.REVIEW_READY,
        payload={"intent_text": "publish the spec"},
    )
    session.add(run)
    await session.flush()
    summary = "Quarterly Spec\nThe spec body, line two.\nLine three."
    deliverable = Deliverable(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        deliverable_type=DeliverableType.CODE,
        payload={"artifact_refs": ["spec.md"], "summary": summary},
    )
    session.add(deliverable)
    await session.flush()
    session.add(
        DeliveryEventRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            deliverable_id=deliverable.id,
            artifact_type=DeliverableType.CODE.value,
            payload={},
            created_at=datetime.now(tz=UTC),
        )
    )
    await session.commit()
    return deliverable.id


async def _seed_notion_connector(
    session: AsyncSession, cipher: CredentialCipher, workspace_id: uuid.UUID
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="notion",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("secret_notion_token"),
            delivery_config={"parent_page_id": "P", "notion_api_url": NOTION_API},
            is_active=True,
        )
    )
    await session.commit()


# --------------------------------------------------------------------------
# The production dependency builds the connector adapter (not the old plain one).
# --------------------------------------------------------------------------


async def test_get_delivery_dispatcher_builds_connector_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un-overridden, the approve route's dispatcher dependency must build a
    :class:`ConnectorDeliveryAdapter` — the SAME adapter the Direct path uses —
    so an approved delivery shapes + delivers the connector event (rather than
    the old plain :class:`RealPluginDispatchAdapter` that skipped connector
    shaping)."""
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    try:
        dispatcher = await get_delivery_dispatcher()
        assert isinstance(dispatcher, ConnectorDeliveryAdapter)
        # It carries the loaded connector plugins (notion among them) + a cipher.
        assert "notion" in dispatcher.plugins_by_name
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Safe Mode ON → enqueue → approve → shaped Notion delivery.
# --------------------------------------------------------------------------


@respx.mock
async def test_safe_mode_approve_delivers_shaped_notion_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    cipher: CredentialCipher,
) -> None:
    route = respx.post(f"{NOTION_API}/v1/pages").mock(
        return_value=httpx.Response(200, json={"id": "page-77", "url": "https://notion.so/page-77"})
    )

    async with sf() as s:
        await _seed_notion_connector(s, cipher, workspace_id)
        deliverable_id = await _seed_verified_deliverable(s, workspace_id)

    # 1. DeliveryWorker drains the event → enqueues PENDING (Safe Mode held it).
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=build_connector_delivery_adapter(
            session_factory=sf, plugins=await _plugins(), cipher=cipher
        ),
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1
    assert not route.called  # Safe Mode → no delivery yet.

    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 1
        assert items[0].status is SafeModeStatus.PENDING

    # 2. POST approve → ConnectorDeliveryAdapter shapes + POSTs the Notion event.
    resp = await client.get("/api/v1/safemode/queue")
    item_id = resp.json()[0]["id"]
    approve = await client.post(f"/api/v1/safemode/{item_id}/approve")
    assert approve.status_code == 200, approve.text
    assert approve.json() == {"item_id": item_id, "status": "approved", "dispatched": True}

    # 3. The shaped Notion outbound was invoked: routing from config, content
    #    from the deliverable.
    assert route.called
    body = route.calls.last.request.content.decode()
    assert '"page_id": "P"' in body or '"page_id":"P"' in body
    assert "Quarterly Spec" in body  # title = first line of the summary
    assert "The spec body" in body  # body carries the rest of the summary

    # 4. Item marked APPROVED.
    async with sf() as s:
        item = await s.get(SafeModeQueueItemRow, uuid.UUID(item_id))
        assert item is not None
        assert item.status is SafeModeStatus.APPROVED
        assert item.decided_at is not None
        # The in-app Deliverable is untouched.
        assert await s.get(Deliverable, deliverable_id) is not None


# --------------------------------------------------------------------------
# Safe Mode ON, NO connector binding → approve succeeds, no external call.
# --------------------------------------------------------------------------


@respx.mock
async def test_safe_mode_approve_no_binding_no_external_call(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    route = respx.post(f"{NOTION_API}/v1/pages")

    async with sf() as s:
        deliverable_id = await _seed_verified_deliverable(s, workspace_id)

    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=build_connector_delivery_adapter(
            session_factory=sf, plugins=await _plugins(), cipher=CredentialCipher(TEST_KEY)
        ),
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    resp = await client.get("/api/v1/safemode/queue")
    item_id = resp.json()[0]["id"]
    approve = await client.post(f"/api/v1/safemode/{item_id}/approve")
    assert approve.status_code == 200, approve.text
    assert approve.json() == {"item_id": item_id, "status": "approved", "dispatched": True}

    # No connector binding → no external HTTP call, no error.
    assert not route.called

    async with sf() as s:
        item = await s.get(SafeModeQueueItemRow, uuid.UUID(item_id))
        assert item is not None
        assert item.status is SafeModeStatus.APPROVED
        # The in-app Deliverable is untouched.
        assert await s.get(Deliverable, deliverable_id) is not None
