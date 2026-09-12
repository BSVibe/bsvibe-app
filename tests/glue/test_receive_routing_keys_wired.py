"""The webhook route and the Receive stage must meet — measured, they never did.

``backend.workflow.application.stages.intake.receive`` resolves an inbound
webhook to its binding using two payload keys (``connector_account_id`` /
``resource_id``) and, *below* that lookup, applies the binding's
``trigger.filters``. Prod measurement (2026-09-12, read-only):

    select count(*),
           count(*) filter (where payload::jsonb ? 'connector_account_id'),
           count(*) filter (where payload::jsonb ? 'resource_id')
    from trigger_events where trigger_kind='webhook';   -- 13 | 0 | 0

Zero of thirteen. The binding branch has NEVER run in production, so
``trigger.filters`` — settable today via ``ResourceBindingCreate/Update.trigger``
and the ``bsvibe_bindings_*`` MCP tools — has never been applied either. PR #924
deleted the sibling knob ``trigger.enabled`` on the stated ground that *"the
Receive stage consumes ``filters`` only"* and *"``filters`` already expresses
'do I act'"*; that described something inert.

PR #922 taught the route to resolve the binding (for ``product_id``) but never
put the routing keys into the STORED payload, which is why ``receive()`` still
saw nothing. Every existing test calls one half directly — the route helper, or
``receive()`` with a hand-built payload — which is exactly how the gap survived.
The seam test below is the one that proves the two halves meet.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher, get_webhook_parser_registry
from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.webhook_registry import WebhookParserRegistry
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher
from backend.shared.wire_kinds import (
    PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID,
    PAYLOAD_KEY_RESOURCE_ID,
)
from backend.workflow.application.stages.intake import receive
from backend.workflow.infrastructure.intake.db import TriggerEventRow
from plugin.telegram.webhook import SECRET_TOKEN_HEADER, parse_update

from .._support import db_engine

# ``asyncio_mode = "auto"`` (pyproject) already runs the async tests here; a
# module-level ``pytest.mark.asyncio`` would only warn on the sync guards below.

TEST_KEY = b"0123456789abcdef0123456789abcdef"
SECRET = "telegram-webhook-secret"

#: The founder's prod binding key — telegram sends it as a JSON NUMBER while the
#: binding row stores the string ``"8242700007"``.
BOUND_CHAT_ID = 8242700007


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest_asyncio.fixture
async def client(sf: Any, cipher: CredentialCipher) -> Any:
    app = create_app()

    async def _session() -> Any:
        async with sf() as s:
            yield s

    registry = WebhookParserRegistry()
    registry.register("telegram", parse_update)

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    app.dependency_overrides[get_webhook_parser_registry] = lambda: registry

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_account(
    session: AsyncSession, cipher: CredentialCipher, *, workspace_id: uuid.UUID
) -> ConnectorAccountRow:
    """A workspace + an active telegram connector account (no binding yet)."""
    session.add(WorkspaceRow(id=workspace_id, name="WS", language="ko"))
    # Flush each FK LEVEL before the rows that point at it — SQLAlchemy orders
    # inserts per-mapper, not by cross-model FK dependency, and SQLite's lax FK
    # enforcement hides every violation here until PostgreSQL runs it.
    await session.flush()
    account = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        connector="telegram",
        webhook_token="wht_" + uuid.uuid4().hex,
        signing_secret_ciphertext=cipher.encrypt("bot:token"),
        delivery_config={"webhook_secret": SECRET},
        is_active=True,
    )
    session.add(account)
    await session.commit()
    return account


async def _seed_binding(
    session: AsyncSession,
    *,
    account: ConnectorAccountRow,
    resource_id: str,
    trigger: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> uuid.UUID:
    """A product bound to ``(account, resource_id)``. Returns the binding id."""
    product = ProductRow(
        id=uuid.uuid4(),
        workspace_id=account.workspace_id,
        name="BStockReport",
        slug=f"p-{uuid.uuid4().hex[:8]}",
    )
    session.add(product)
    await session.flush()
    binding_id = uuid.uuid4()
    session.add(
        ResourceBindingRow(
            id=binding_id,
            workspace_id=account.workspace_id,
            product_id=product.id,
            connector_account_id=account.id,
            resource_id=resource_id,
            trigger=trigger if trigger is not None else {"filters": {}},
            selection=selection if selection is not None else {},
        )
    )
    await session.commit()
    return binding_id


def _telegram_body(*, update_id: int, chat_id: int, text: str) -> bytes:
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": chat_id},
                "from": {"id": 7, "is_bot": False},
                "text": text,
            },
        }
    ).encode()


async def _post(
    client: httpx.AsyncClient, account: ConnectorAccountRow, body: bytes
) -> httpx.Response:
    return await client.post(
        f"/api/webhooks/telegram/{account.webhook_token}",
        content=body,
        headers={SECRET_TOKEN_HEADER: SECRET, "Content-Type": "application/json"},
    )


async def _stored_rows(sf: Any) -> list[TriggerEventRow]:
    async with sf() as s:
        return list((await s.execute(select(TriggerEventRow))).scalars().all())


# ── 1 / 2. what the route writes into the stored payload ─────────────────────


async def test_a_bound_delivery_stores_both_routing_keys(sf: Any, client: Any, cipher) -> None:
    """The route resolved a binding → the stored payload carries the keys the
    Receive stage looks for, with ``resource_id`` STRINGIFIED.

    Telegram sends ``chat_id`` as a JSON number; the founder typed a string into
    the binding. Storing the raw number would make ``receive()`` fall straight
    back to pass-through — the same silent failure, one hop later.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        await _seed_binding(s, account=account, resource_id=str(BOUND_CHAT_ID))

    resp = await _post(
        client, account, _telegram_body(update_id=1, chat_id=BOUND_CHAT_ID, text="가자")
    )
    assert resp.status_code == 202, resp.text

    rows = await _stored_rows(sf)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload[PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID] == str(account.id)
    assert payload[PAYLOAD_KEY_RESOURCE_ID] == str(BOUND_CHAT_ID)
    assert isinstance(payload[PAYLOAD_KEY_RESOURCE_ID], str)


async def test_an_unbound_delivery_stores_neither_routing_key(sf: Any, client: Any, cipher) -> None:
    """No binding matched → the payload is left exactly as the parser built it.

    Pass-through is today's behaviour for an unbound chat and must stay it; a
    stamped key with no binding behind it would make ``receive()`` do a lookup
    that can only miss.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        # A binding exists — for a DIFFERENT chat. This is the prod shape: one
        # bound 1:1 chat, every group chat unbound.
        await _seed_binding(s, account=account, resource_id="some-other-chat")

    resp = await _post(client, account, _telegram_body(update_id=2, chat_id=-100333, text="hi"))
    assert resp.status_code == 202, resp.text

    rows = await _stored_rows(sf)
    assert len(rows) == 1
    payload = rows[0].payload
    assert PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID not in payload
    assert PAYLOAD_KEY_RESOURCE_ID not in payload


# ── 3. the seam: the row the route wrote, read by the stage ──────────────────


async def test_the_stored_event_reaches_the_binding_branch_in_receive(
    sf: Any, client: Any, cipher
) -> None:
    """The test that proves the two halves meet.

    Every other test in this area drives ONE half: the route helper directly, or
    ``receive()`` with a hand-written payload. Both were green the whole time the
    binding branch was dead in production. So: post through the real route, load
    the row it actually persisted, and hand THAT row to ``receive()``.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        binding_id = await _seed_binding(
            s,
            account=account,
            resource_id=str(BOUND_CHAT_ID),
            selection={"artifact_type": "report"},
        )

    resp = await _post(
        client, account, _telegram_body(update_id=3, chat_id=BOUND_CHAT_ID, text="리포트")
    )
    assert resp.status_code == 202, resp.text

    async with sf() as s:
        row = (await s.execute(select(TriggerEventRow))).scalars().one()
        outcome = await receive(s, row)

    assert outcome.filtered_out is False
    assert outcome.binding_id == binding_id, "receive() fell back to pass-through"
    assert outcome.suggested_artifact_type == "report"
    assert outcome.request_payload["binding_id"] == str(binding_id)
    assert outcome.request_payload["selection"] == {"artifact_type": "report"}


# ── 4. filters now actually filter ───────────────────────────────────────────


async def test_a_non_matching_filter_drops_the_event(sf: Any, client: Any, cipher) -> None:
    """A binding filter that does not match the delivery rejects it.

    This assertion could not have held before this PR at ANY filter value: the
    branch that reads ``trigger.filters`` was unreachable from the route.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        await _seed_binding(
            s,
            account=account,
            resource_id=str(BOUND_CHAT_ID),
            trigger={"filters": {"telegram_update": "channel_post"}},
        )

    resp = await _post(
        client, account, _telegram_body(update_id=4, chat_id=BOUND_CHAT_ID, text="x")
    )
    assert resp.status_code == 202, resp.text

    async with sf() as s:
        row = (await s.execute(select(TriggerEventRow))).scalars().one()
        outcome = await receive(s, row)

    assert outcome.filtered_out is True
    assert outcome.reason == "filter_rejected"


async def test_a_matching_filter_passes_the_event(sf: Any, client: Any, cipher) -> None:
    """The positive control for the test above — same filter KEY, matching value.

    Without this, a filter implementation that rejected everything would pass the
    rejection test and look correct.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        binding_id = await _seed_binding(
            s,
            account=account,
            resource_id=str(BOUND_CHAT_ID),
            trigger={"filters": {"telegram_update": "message"}},
        )

    resp = await _post(
        client, account, _telegram_body(update_id=5, chat_id=BOUND_CHAT_ID, text="y")
    )
    assert resp.status_code == 202, resp.text

    async with sf() as s:
        row = (await s.execute(select(TriggerEventRow))).scalars().one()
        outcome = await receive(s, row)

    assert outcome.filtered_out is False
    assert outcome.binding_id == binding_id


async def test_an_empty_filter_still_acts_on_everything(sf: Any, client: Any, cipher) -> None:
    """The no-regression guard for prod.

    Both live bindings carry ``{"filters": {}}`` (re-verified 2026-09-12 against
    the prod DB), so wiring the lookup up must be behaviour-neutral for them:
    every delivery still passes, now WITH the product/binding routing hints it
    was always supposed to carry.
    """
    ws = uuid.uuid4()
    async with sf() as s:
        account = await _seed_account(s, cipher, workspace_id=ws)
        binding_id = await _seed_binding(
            s, account=account, resource_id=str(BOUND_CHAT_ID), trigger={"filters": {}}
        )

    resp = await _post(
        client, account, _telegram_body(update_id=6, chat_id=BOUND_CHAT_ID, text="z")
    )
    assert resp.status_code == 202, resp.text

    async with sf() as s:
        row = (await s.execute(select(TriggerEventRow))).scalars().one()
        outcome = await receive(s, row)

    assert outcome.filtered_out is False
    assert outcome.binding_id == binding_id


# ── 5. one definition of the key names ───────────────────────────────────────


_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two ends of this wire contract, pinned as a FILE SET rather than a
#: pattern: a future edit that renames one side only has to introduce the
#: literal here, and that is what fails.
_WIRE_ENDS = (
    _REPO_ROOT / "backend" / "api" / "webhooks.py",
    _REPO_ROOT / "backend" / "workflow" / "application" / "stages" / "intake.py",
)


def test_both_ends_reference_one_definition_of_the_routing_keys() -> None:
    """Neither end may spell the key names itself.

    Two spellings of a wire contract is how the two ends drift apart — and this
    particular contract already spent its whole life broken at exactly this
    seam, with the producer never writing what the consumer read.
    """
    assert _WIRE_ENDS, "the wire-end file set is empty — this guard would pass vacuously"
    literal = re.compile(r"""["'](?:connector_account_id|resource_id)["']""")
    offenders: dict[str, list[str]] = {}
    for path in _WIRE_ENDS:
        assert path.is_file(), f"pinned wire end has moved: {path}"
        hits = [line.strip() for line in path.read_text().splitlines() if literal.search(line)]
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "these modules spell a routing key themselves instead of importing the "
        f"single definition in backend.shared.wire_kinds: {offenders}"
    )


def test_the_single_definition_holds_the_measured_wire_values() -> None:
    """The values ``receive()`` has always looked for — and prod's 13 webhook
    events never carried."""
    assert PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID == "connector_account_id"
    assert PAYLOAD_KEY_RESOURCE_ID == "resource_id"
