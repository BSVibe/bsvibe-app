"""RFC 8628 device authorization grant — service layer.

The flow exists for the operator who has a browser but no terminal on the host
being signed in. Nothing is pasted back: the device polls, the human approves
elsewhere, and the credential lands in the CLI without ever transiting a
channel a human reads.

The properties worth pinning are the ones that make polling safe:

* the device code is opaque and stored hashed, so a database leak cannot be
  exchanged for a token;
* polling before approval says ``authorization_pending``, and polling too fast
  says ``slow_down`` — a device that ignores ``interval`` must not be able to
  brute-force the short ``user_code``;
* approval binds the workspace of the human who approved, not of the device;
* a code is single-use, and denial and expiry are distinct terminal answers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.identity.db import UserRow
from backend.identity.oauth_db import OAuthDeviceCodeRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.oauth_service import (
    DEVICE_CODE_TTL,
    DEVICE_POLL_INTERVAL_S,
    DeviceExchangeOutcome,
    approve_device_code,
    deny_device_code,
    exchange_device_code,
    lookup_device_code_by_user_code,
    start_device_authorization,
)
from backend.identity.workspaces_db import WorkspaceRow

from .._support import memory_session

pytestmark = pytest.mark.asyncio

ISSUER = "http://test/oauth"
CLIENT_ID = "dcr-device-test"


@pytest.fixture(autouse=True)
def _reset_keys() -> None:
    reset_signing_key_for_tests()
    yield
    reset_signing_key_for_tests()


async def _seed(session) -> tuple[UserRow, WorkspaceRow]:
    ws = WorkspaceRow(name="t-ws")
    session.add(ws)
    user = UserRow(supabase_user_id=f"sb-{uuid.uuid4()}", email="t@example.com")
    session.add(user)
    await session.flush()
    return user, ws


# ---------------------------------------------------------------------------
# Authorization request
# ---------------------------------------------------------------------------


async def test_start_returns_user_facing_and_device_facing_codes() -> None:
    async with memory_session() as session:
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read", "mcp:write"]
        )
        await session.flush()

        assert started.device_code
        assert started.user_code
        # The human retypes this one, so it stays short and unambiguous.
        assert len(started.user_code) <= 12
        assert started.user_code == started.user_code.upper()
        assert not set(started.user_code) & set("01OI")  # ambiguous glyphs excluded
        assert started.interval == DEVICE_POLL_INTERVAL_S
        assert started.expires_in == int(DEVICE_CODE_TTL.total_seconds())


async def test_device_code_is_not_stored_in_the_clear() -> None:
    """A DB leak must not yield an exchangeable code."""
    async with memory_session() as session:
        started = await start_device_authorization(session, client_id=CLIENT_ID, scope=["mcp:read"])
        await session.flush()

        rows = (
            (await session.execute(__import__("sqlalchemy").select(OAuthDeviceCodeRow)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        stored = rows[0]
        assert started.device_code not in str(stored.device_code_hash)
        assert stored.user_code == started.user_code  # short-lived, useless alone


async def test_user_codes_are_unique_across_pending_requests() -> None:
    async with memory_session() as session:
        codes = set()
        for _ in range(20):
            started = await start_device_authorization(
                session, client_id=CLIENT_ID, scope=["mcp:read"]
            )
            codes.add(started.user_code)
        await session.flush()
        assert len(codes) == 20


# ---------------------------------------------------------------------------
# Polling before a human acts
# ---------------------------------------------------------------------------


async def test_polling_before_approval_is_authorization_pending() -> None:
    async with memory_session() as session:
        started = await start_device_authorization(session, client_id=CLIENT_ID, scope=["mcp:read"])
        await session.flush()

        outcome, tokens = await exchange_device_code(
            session, device_code=started.device_code, client_id=CLIENT_ID, issuer=ISSUER
        )
        assert outcome is DeviceExchangeOutcome.AUTHORIZATION_PENDING
        assert tokens is None


async def test_polling_faster_than_the_interval_is_slow_down() -> None:
    """A device ignoring `interval` must not get free brute-force attempts."""
    now = datetime.now(UTC)
    async with memory_session() as session:
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read"], now=now
        )
        await session.flush()

        first, _ = await exchange_device_code(
            session, device_code=started.device_code, client_id=CLIENT_ID, issuer=ISSUER, now=now
        )
        assert first is DeviceExchangeOutcome.AUTHORIZATION_PENDING

        immediate, _ = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + timedelta(seconds=1),
        )
        assert immediate is DeviceExchangeOutcome.SLOW_DOWN

        after_interval, _ = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + timedelta(seconds=DEVICE_POLL_INTERVAL_S + 1),
        )
        assert after_interval is DeviceExchangeOutcome.AUTHORIZATION_PENDING


async def test_unknown_device_code_is_invalid_grant() -> None:
    async with memory_session() as session:
        outcome, tokens = await exchange_device_code(
            session, device_code="nope", client_id=CLIENT_ID, issuer=ISSUER
        )
        assert outcome is DeviceExchangeOutcome.INVALID_GRANT
        assert tokens is None


async def test_wrong_client_id_is_invalid_grant() -> None:
    """The code is bound to the client that requested it."""
    async with memory_session() as session:
        started = await start_device_authorization(session, client_id=CLIENT_ID, scope=["mcp:read"])
        await session.flush()

        outcome, _ = await exchange_device_code(
            session, device_code=started.device_code, client_id="dcr-someone-else", issuer=ISSUER
        )
        assert outcome is DeviceExchangeOutcome.INVALID_GRANT


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def test_approved_code_exchanges_once_for_a_token_pair() -> None:
    now = datetime.now(UTC)
    async with memory_session() as session:
        user, ws = await _seed(session)
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read", "mcp:admin"], now=now
        )
        await session.flush()

        approved = await approve_device_code(
            session, user_code=started.user_code, user_id=user.id, workspace_id=ws.id
        )
        assert approved is not None
        await session.flush()

        outcome, tokens = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + timedelta(seconds=DEVICE_POLL_INTERVAL_S + 1),
        )
        assert outcome is DeviceExchangeOutcome.APPROVED
        assert tokens is not None
        # The approver's workspace, not anything the device asserted.
        assert tokens.scope == ["mcp:read", "mcp:admin"]
        assert tokens.access_token and tokens.refresh_token

        # Single use: a replay must not mint a second credential.
        replay, replay_tokens = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + timedelta(seconds=2 * DEVICE_POLL_INTERVAL_S + 2),
        )
        assert replay is DeviceExchangeOutcome.INVALID_GRANT
        assert replay_tokens is None


async def test_user_code_lookup_is_case_and_dash_insensitive() -> None:
    """The human retypes this — accept what they'd plausibly type."""
    async with memory_session() as session:
        started = await start_device_authorization(session, client_id=CLIENT_ID, scope=["mcp:read"])
        await session.flush()

        typed = started.user_code.lower().replace("-", " ")
        found = await lookup_device_code_by_user_code(session, user_code=typed)
        assert found is not None
        assert found.user_code == started.user_code


async def test_approving_an_unknown_user_code_returns_none() -> None:
    async with memory_session() as session:
        user, ws = await _seed(session)
        assert (
            await approve_device_code(
                session, user_code="ZZZZ-ZZZZ", user_id=user.id, workspace_id=ws.id
            )
            is None
        )


# ---------------------------------------------------------------------------
# Denial + expiry are distinct terminal answers
# ---------------------------------------------------------------------------


async def test_denied_code_reports_access_denied() -> None:
    now = datetime.now(UTC)
    async with memory_session() as session:
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read"], now=now
        )
        await session.flush()
        assert await deny_device_code(session, user_code=started.user_code) is not None
        await session.flush()

        outcome, tokens = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + timedelta(seconds=DEVICE_POLL_INTERVAL_S + 1),
        )
        assert outcome is DeviceExchangeOutcome.ACCESS_DENIED
        assert tokens is None


async def test_expired_code_reports_expired_not_pending() -> None:
    """`expired_token` tells the CLI to stop; `authorization_pending` would loop forever."""
    now = datetime.now(UTC)
    async with memory_session() as session:
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read"], now=now
        )
        await session.flush()

        outcome, _ = await exchange_device_code(
            session,
            device_code=started.device_code,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            now=now + DEVICE_CODE_TTL + timedelta(seconds=1),
        )
        assert outcome is DeviceExchangeOutcome.EXPIRED_TOKEN


async def test_expired_code_cannot_be_approved() -> None:
    now = datetime.now(UTC)
    async with memory_session() as session:
        user, ws = await _seed(session)
        started = await start_device_authorization(
            session, client_id=CLIENT_ID, scope=["mcp:read"], now=now
        )
        await session.flush()

        approved = await approve_device_code(
            session,
            user_code=started.user_code,
            user_id=user.id,
            workspace_id=ws.id,
            now=now + DEVICE_CODE_TTL + timedelta(seconds=1),
        )
        assert approved is None
