"""Connector delivery end-to-end — verified Deliverable → external Notion page.

This closes the last loop of the Direct path (Workflow §11.1 / §12.5 #8): a
verified run produces a :class:`~backend.execution.db.Deliverable` and a
:class:`~backend.delivery.db.DeliveryEventRow`; the
:class:`~backend.workers.delivery_worker.DeliveryWorker` drains it through a
:class:`~backend.delivery.connector_dispatch.ConnectorDeliveryAdapter` that
resolves the workspace's configured ``connector_accounts`` (binding =
``delivery_config``), shapes the connector's outbound event from the
deliverable content + the stable routing config, and dispatches it through the
real :class:`~backend.delivery.dispatcher.DeliveryDispatcher` over the loaded
plugins.

The Notion HTTP API is mocked with respx — no real network I/O. The work LLM /
sandbox path is bypassed: we seed a verified Deliverable + DeliveryEventRow
directly (the orchestrator side is already proved by
``test_direct_path_e2e``), and assert the *delivery* leg.

Runs on in-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL``
is set (mirrors the other glue tests).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.accounts.crypto import CredentialCipher
from backend.connectors.db import ConnectorAccountRow
from backend.delivery.connector_dispatch import build_connector_delivery_adapter
from backend.delivery.db import DeliveryEventRow
from backend.execution.db import Deliverable, DeliverableType, ExecutionRun, RunStatus
from backend.plugins.implementations.notion import plugin as notion_module
from backend.plugins.loader import PluginLoader
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig

from .._support import db_engine

NOTION_API = "https://api.notion.test"

# Deterministic 32-byte AES key for CredentialCipher in tests (matches the
# webhook/connector glue tests' pattern).
TEST_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    """A deterministic test cipher (32-byte key) — matches the CRUD encrypt."""
    return CredentialCipher(TEST_KEY)


async def _seed_verified_deliverable(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    """Seed a verified ExecutionRun + Deliverable + DeliveryEventRow."""
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
            payload={"artifact_refs": ["spec.md"], "summary": summary[:500]},
        )
    )
    await session.commit()
    return deliverable.id


async def _seed_notion_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="notion",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("secret_notion_token"),
            delivery_config={
                "parent_page_id": "P",
                "notion_api_url": NOTION_API,
            },
            is_active=True,
        )
    )
    await session.commit()


async def _plugins():
    impl_dir = Path(notion_module.__file__).resolve().parents[1]
    return await PluginLoader(impl_dir).load_all()


@respx.mock
async def test_verified_deliverable_delivers_to_notion(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{NOTION_API}/v1/pages").mock(
        return_value=httpx.Response(200, json={"id": "page-77", "url": "https://notion.so/page-77"})
    )

    async with sf() as s:
        await _seed_notion_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # Notion POST /v1/pages was called with the parent from config + a title/body
    # derived from the deliverable summary.
    assert route.called
    body = route.calls.last.request.content.decode()
    assert '"page_id": "P"' in body or '"page_id":"P"' in body
    assert "Quarterly Spec" in body  # title = first line of the summary
    assert "The spec body" in body  # body carries the rest of the summary

    # Event drained from the queue.
    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


@respx.mock
async def test_no_connector_account_no_external_call_no_error(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """No configured connector_account → the Deliverable still exists, the
    event still drains, and NO external HTTP call is made (no error)."""
    workspace_id = uuid.uuid4()
    route = respx.post(f"{NOTION_API}/v1/pages")

    async with sf() as s:
        deliverable_id = await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1
    assert not route.called

    async with sf() as s:
        # The in-app Deliverable is untouched.
        deliverable = await s.get(Deliverable, deliverable_id)
        assert deliverable is not None
        # Event drained.
        assert (await s.execute(select(DeliveryEventRow))).first() is None


@respx.mock
async def test_inactive_connector_is_skipped(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """A revoked (is_active=False) connector_account is not delivered to."""
    workspace_id = uuid.uuid4()
    route = respx.post(f"{NOTION_API}/v1/pages")

    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="notion",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("secret_notion_token"),
                delivery_config={"parent_page_id": "P", "notion_api_url": NOTION_API},
                is_active=False,
            )
        )
        await s.commit()
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1
    assert not route.called


# ── slack ──────────────────────────────────────────────────────────────────

SLACK_API = "https://slack.test/api"


async def _seed_slack_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="slack",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("xoxb-test-bot-token"),
            delivery_config={"channel": "C123", "slack_api_url": SLACK_API},
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_slack(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{SLACK_API}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "171.99", "channel": "C123"})
    )

    async with sf() as s:
        await _seed_slack_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # chat.postMessage was called with the channel from config + text derived
    # from the deliverable summary (routing-from-config, content-from-content).
    assert route.called
    body = route.calls.last.request.content.decode()
    assert '"channel": "C123"' in body or '"channel":"C123"' in body
    assert "Quarterly Spec" in body
    assert "The spec body" in body
    # Bearer token came from the decrypted connector secret (bot_token slot).
    assert route.calls.last.request.headers["authorization"] == "Bearer xoxb-test-bot-token"

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


@respx.mock
async def test_slack_missing_channel_soft_fails_no_call_no_wedge(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """A misconfigured slack target (no ``channel``) makes NO external call and
    does not wedge the queue — the builder ValueError soft-fails into a failed
    action and the event still drains (mirrors notion's missing parent_page_id).
    """
    workspace_id = uuid.uuid4()
    route = respx.post(f"{SLACK_API}/chat.postMessage")

    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="slack",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("xoxb-test-bot-token"),
                delivery_config={"slack_api_url": SLACK_API},  # no channel
                is_active=True,
            )
        )
        await s.commit()
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1
    assert not route.called

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# ── email ────────────────────────────────────────────────────────────────────

RESEND_API = "https://resend.test"


async def _seed_email_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="email-sender",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("re_test_api_key"),
            delivery_config={
                "to": "ceo@bsvibe.dev",
                "from": "BSVibe <noreply@bsvibe.dev>",
                "resend_api_url": RESEND_API,
            },
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_email(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{RESEND_API}/emails").mock(
        return_value=httpx.Response(200, json={"id": "email-42"})
    )

    async with sf() as s:
        await _seed_email_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # POST /emails was called with to/from from config + subject = first summary
    # line + body = summary (sent as text).
    assert route.called
    body = route.calls.last.request.content.decode()
    assert '"to": "ceo@bsvibe.dev"' in body or '"to":"ceo@bsvibe.dev"' in body
    assert "Quarterly Spec" in body  # subject = first line of summary
    assert "The spec body" in body  # body carries the summary
    assert "noreply@bsvibe.dev" in body  # founder-set sender from config
    # Bearer token came from the decrypted connector secret (api_key slot).
    assert route.calls.last.request.headers["authorization"] == "Bearer re_test_api_key"

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# ── telegram ───────────────────────────────────────────────────────────────────

TELEGRAM_API = "https://telegram.test"


async def _seed_telegram_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="telegram",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("123:bot-token"),
            delivery_config={"chat_id": "555", "telegram_api_url": TELEGRAM_API},
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_telegram(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    # The bot token is embedded in the URL path (/bot<token>/sendMessage).
    route = respx.post(f"{TELEGRAM_API}/bot123:bot-token/sendMessage").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": 555}}}
        )
    )

    async with sf() as s:
        await _seed_telegram_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # sendMessage was called with chat_id from config + text from the summary.
    assert route.called
    body = route.calls.last.request.content.decode()
    assert '"chat_id": "555"' in body or '"chat_id":"555"' in body
    assert "Quarterly Spec" in body
    assert "The spec body" in body

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


@respx.mock
async def test_telegram_missing_chat_id_soft_fails_no_call_no_wedge(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """A misconfigured telegram target (no ``chat_id``) makes NO external call
    and does not wedge the queue — the builder ValueError soft-fails and the
    event still drains (mirrors slack's missing channel)."""
    workspace_id = uuid.uuid4()
    route = respx.post(url__regex=rf"{TELEGRAM_API}/bot.*")

    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("123:bot-token"),
                delivery_config={"telegram_api_url": TELEGRAM_API},  # no chat_id
                is_active=True,
            )
        )
        await s.commit()
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1
    assert not route.called

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# ── discord ────────────────────────────────────────────────────────────────────

DISCORD_API = "https://discord.test/api"


async def _seed_discord_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="discord",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("discord-bot-token"),
            delivery_config={"channel_id": "C99", "discord_api_url": DISCORD_API},
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_discord(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{DISCORD_API}/channels/C99/messages").mock(
        return_value=httpx.Response(200, json={"id": "m-7", "channel_id": "C99"})
    )

    async with sf() as s:
        await _seed_discord_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # POST to the channel from config with content from the summary.
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "Quarterly Spec" in body
    assert "The spec body" in body
    # Bot token came from the decrypted connector secret (bot_token slot).
    assert route.calls.last.request.headers["authorization"] == "Bot discord-bot-token"

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# ── linear ─────────────────────────────────────────────────────────────────────

LINEAR_API = "https://linear.test"


async def _seed_linear_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="linear",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("lin_api_secret"),
            delivery_config={"team_id": "TEAM-7", "linear_api_url": LINEAR_API},
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_linear(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{LINEAR_API}/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "iss-1",
                            "identifier": "ENG-1",
                            "url": "https://linear.app/iss-1",
                        },
                    }
                }
            },
        )
    )

    async with sf() as s:
        await _seed_linear_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # issueCreate mutation carried the team from config + title/description from
    # the summary.
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "TEAM-7" in body
    assert "Quarterly Spec" in body
    assert "The spec body" in body
    # Linear personal API keys are sent RAW (no "Bearer ") in Authorization.
    assert route.calls.last.request.headers["authorization"] == "lin_api_secret"

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# ── trello (dual-secret) ─────────────────────────────────────────────────────────

TRELLO_API = "https://trello.test"


async def _seed_trello_connector(
    session: AsyncSession,
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    # Dual-secret: the connector_account stores ONLY the secret token; the
    # non-secret api_key rides in the founder-set delivery_config.
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="trello",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("trello-secret-token"),
            delivery_config={
                "list_id": "LIST-9",
                "api_key": "trello-app-key",
                "trello_api_url": TRELLO_API,
            },
            is_active=True,
        )
    )
    await session.commit()


@respx.mock
async def test_verified_deliverable_delivers_to_trello(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    workspace_id = uuid.uuid4()
    route = respx.post(f"{TRELLO_API}/1/cards").mock(
        return_value=httpx.Response(
            200, json={"id": "card-1", "shortUrl": "https://trello.com/c/card-1"}
        )
    )

    async with sf() as s:
        await _seed_trello_connector(s, cipher, workspace_id)
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1

    # POST /1/cards with the list from config + name/desc from the summary, and
    # BOTH trello auth query params present — the secret token from the decrypted
    # connector secret + the app key from delivery_config (dual-secret mapping).
    assert route.called
    req = route.calls.last.request
    assert req.url.params["idList"] == "LIST-9"
    assert req.url.params["name"] == "Quarterly Spec"
    assert "The spec body" in req.url.params["desc"]
    assert req.url.params["key"] == "trello-app-key"
    assert req.url.params["token"] == "trello-secret-token"

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


@respx.mock
async def test_trello_missing_api_key_soft_fails_no_call_no_wedge(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """A trello target missing the founder-set ``api_key`` (dual-secret) makes
    NO external call and does not wedge the queue — the builder ValueError
    soft-fails and the event still drains."""
    workspace_id = uuid.uuid4()
    route = respx.post(f"{TRELLO_API}/1/cards")

    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="trello",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext=cipher.encrypt("trello-secret-token"),
                delivery_config={"list_id": "LIST-9", "trello_api_url": TRELLO_API},  # no api_key
                is_active=True,
            )
        )
        await s.commit()
        await _seed_verified_deliverable(s, workspace_id)

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf, plugins=list(registry.values()), cipher=cipher
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )

    assert await worker.drain_once() == 1
    assert not route.called

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None
