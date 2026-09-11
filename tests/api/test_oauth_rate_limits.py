"""Abuse limits on the unauthenticated OAuth surface — and the key they use.

Two things are pinned here.

**The key.** Before this, every per-IP limiter keyed on
``request.client.host``, which ``ProxyHeadersMiddleware(trusted_hosts="*")``
derives from the FIRST ``X-Forwarded-For`` entry. Cloudflare APPENDS the real
peer to that header rather than overwriting it, so the first entry is whatever
the caller typed: one header bought a fresh bucket per request, and naming a
victim's IP poisoned *their* bucket. The limiters now key on
``CF-Connecting-IP``, which Cloudflare sets.

**The shape on /token.** The device grant polls ``/token`` every
``DEVICE_POLL_INTERVAL_S`` seconds for the whole time a human takes to
approve — easily 100+ POSTs for ONE legitimate ``bsvibe login``. A request
counter there breaks the CLI, so ``/token`` counts *failed credential
attempts* instead: ``authorization_pending`` / ``slow_down`` are the protocol
working as designed and must never consume budget.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.oauth_db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.oauth import (
    _ANON_DCR_MAX_PER_WINDOW,
    _DEVICE_AUTHZ_MAX_PER_WINDOW,
    _INTROSPECT_MAX_PER_WINDOW,
    _REVOKE_MAX_PER_WINDOW,
    _TOKEN_FAILURE_MAX_PER_WINDOW,
    _reset_oauth_rate_limits_for_tests,
)
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.oauth_keys import reset_signing_key_for_tests
from backend.identity.workspaces_db import WorkspaceRow
from backend.shared.client_ip import CF_CONNECTING_IP_HEADER

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

CLIENT_ID = "dcr-ratelimit-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

#: The honest Cloudflare peer both attacker requests actually come from.
CF_PEER = "203.0.113.9"
#: A second, unrelated caller — used to prove budgets are per-key, not global.
OTHER_PEER = "198.51.100.7"


@pytest.fixture(autouse=True)
def _reset_buckets() -> Any:
    _reset_oauth_rate_limits_for_tests()
    yield
    _reset_oauth_rate_limits_for_tests()


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    monkeypatch.setenv("BSVIBE_OAUTH_ISSUER", "http://test")
    get_settings.cache_clear()
    reset_signing_key_for_tests()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    reset_signing_key_for_tests()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def seeded_user(db: Any, workspace_id: uuid.UUID) -> AsyncIterator[UserRow]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t-ws"))
        user = UserRow(supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.commit()
        yield user


@pytest_asyncio.fixture
async def client(
    db: Any, workspace_id: uuid.UUID, seeded_user: UserRow
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[Any]:
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user_row] = lambda: seeded_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _cf(peer: str, *, forged_xff_head: str | None = None) -> dict[str, str]:
    """Headers as Cloudflare would deliver them, optionally with a forged head.

    Cloudflare APPENDS its view of the peer to whatever ``X-Forwarded-For``
    the caller sent, so ``forged_xff_head`` is the attacker's contribution.
    """
    headers = {CF_CONNECTING_IP_HEADER: peer}
    if forged_xff_head is not None:
        headers["X-Forwarded-For"] = f"{forged_xff_head}, {peer}"
    else:
        headers["X-Forwarded-For"] = peer
    return headers


# ---------------------------------------------------------------------------
# The vulnerability itself
# ---------------------------------------------------------------------------


async def test_forged_forwarded_for_heads_share_one_bucket(client: httpx.AsyncClient) -> None:
    """THE regression test: a per-request forged XFF head must NOT mint a bucket.

    Driven through ``/register`` because its limit (10/hour) is the cheapest to
    exhaust, but the key is shared by every limiter on this surface.
    """
    body = {"client_name": "burst", "redirect_uris": ["http://127.0.0.1/cb"]}
    for i in range(_ANON_DCR_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/register",
            json=body,
            headers=_cf(CF_PEER, forged_xff_head=f"10.0.0.{i}"),
        )
        assert r.status_code == 201, r.text
    # A brand-new forged head, same Cloudflare peer. Pre-fix this was a fresh
    # bucket and answered 201.
    r = await client.post(
        "/api/oauth/register",
        json=body,
        headers=_cf(CF_PEER, forged_xff_head="9.9.9.9"),
    )
    assert r.status_code == 429, r.text


async def test_a_forged_head_cannot_poison_another_callers_bucket(
    client: httpx.AsyncClient,
) -> None:
    """Naming a victim's IP in ``X-Forwarded-For`` must not spend their budget."""
    body = {"client_name": "burst", "redirect_uris": ["http://127.0.0.1/cb"]}
    for _ in range(_ANON_DCR_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/register",
            json=body,
            headers=_cf(CF_PEER, forged_xff_head=OTHER_PEER),
        )
        assert r.status_code == 201, r.text
    # The victim, arriving over their own Cloudflare hop, is untouched.
    r = await client.post("/api/oauth/register", json=body, headers=_cf(OTHER_PEER))
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# /token — failed attempts, not requests
# ---------------------------------------------------------------------------


async def _start_device(client: httpx.AsyncClient, *, peer: str = CF_PEER) -> dict[str, Any]:
    r = await client.post(
        "/api/oauth/device_authorization",
        data={"client_id": CLIENT_ID, "scope": "mcp:read"},
        headers=_cf(peer),
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_device_polling_never_consumes_the_token_budget(
    client: httpx.AsyncClient,
) -> None:
    """``bsvibe login`` protection: 100+ pending polls must ALL get through."""
    started = await _start_device(client)
    polls = _TOKEN_FAILURE_MAX_PER_WINDOW * 2 + 10
    answers: set[str] = set()
    for _ in range(polls):
        r = await client.post(
            "/api/oauth/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": started["device_code"],
                "client_id": CLIENT_ID,
            },
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 400, f"poll answered {r.status_code}: {r.text}"
        answers.add(str(r.json()["error"]))
    assert answers, "the poll loop must have recorded at least one answer"
    assert answers <= {"authorization_pending", "slow_down"}


async def test_failed_credential_attempts_do_trip_the_token_limit(
    client: httpx.AsyncClient,
) -> None:
    """Same endpoint, same IP — guessing does consume budget."""
    for _ in range(_TOKEN_FAILURE_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": "not-a-real-device-code",
                "client_id": CLIENT_ID,
            },
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_grant"
    r = await client.post(
        "/api/oauth/token",
        data={
            "grant_type": DEVICE_GRANT,
            "device_code": "not-a-real-device-code",
            "client_id": CLIENT_ID,
        },
        headers=_cf(CF_PEER),
    )
    assert r.status_code == 429, r.text


async def test_a_malformed_token_request_does_not_consume_budget(
    client: httpx.AsyncClient,
) -> None:
    """Garbage probes fill no bucket — the existing DCR convention, kept."""
    for _ in range(_TOKEN_FAILURE_MAX_PER_WINDOW + 5):
        r = await client.post(
            "/api/oauth/token",
            data={"grant_type": "client_credentials", "client_id": CLIENT_ID},
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "unsupported_grant_type"


async def test_the_token_limit_is_per_key_not_global(client: httpx.AsyncClient) -> None:
    for _ in range(_TOKEN_FAILURE_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": "not-a-real-device-code",
                "client_id": CLIENT_ID,
            },
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 400, r.text
    exhausted = await client.post(
        "/api/oauth/token",
        data={"grant_type": DEVICE_GRANT, "device_code": "x", "client_id": CLIENT_ID},
        headers=_cf(CF_PEER),
    )
    assert exhausted.status_code == 429
    # A different Cloudflare peer still gets a real protocol answer.
    other = await client.post(
        "/api/oauth/token",
        data={"grant_type": DEVICE_GRANT, "device_code": "x", "client_id": CLIENT_ID},
        headers=_cf(OTHER_PEER),
    )
    assert other.status_code == 400, other.text
    assert other.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# /introspect, /revoke, /device_authorization — plain request counts
# ---------------------------------------------------------------------------


async def test_introspect_is_rate_limited(client: httpx.AsyncClient) -> None:
    for _ in range(_INTROSPECT_MAX_PER_WINDOW):
        r = await client.post("/api/oauth/introspect", data={"token": "nope"}, headers=_cf(CF_PEER))
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False
    r = await client.post("/api/oauth/introspect", data={"token": "nope"}, headers=_cf(CF_PEER))
    assert r.status_code == 429, r.text


async def test_introspect_limit_is_per_key(client: httpx.AsyncClient) -> None:
    for _ in range(_INTROSPECT_MAX_PER_WINDOW):
        r = await client.post("/api/oauth/introspect", data={"token": "nope"}, headers=_cf(CF_PEER))
        assert r.status_code == 200, r.text
    assert (
        await client.post("/api/oauth/introspect", data={"token": "n"}, headers=_cf(CF_PEER))
    ).status_code == 429
    assert (
        await client.post("/api/oauth/introspect", data={"token": "n"}, headers=_cf(OTHER_PEER))
    ).status_code == 200


async def test_revoke_is_rate_limited(client: httpx.AsyncClient) -> None:
    for _ in range(_REVOKE_MAX_PER_WINDOW):
        r = await client.post("/api/oauth/revoke", data={"token": "nope"}, headers=_cf(CF_PEER))
        assert r.status_code == 200, r.text
    r = await client.post("/api/oauth/revoke", data={"token": "nope"}, headers=_cf(CF_PEER))
    assert r.status_code == 429, r.text


async def test_revoke_limit_is_per_key(client: httpx.AsyncClient) -> None:
    for _ in range(_REVOKE_MAX_PER_WINDOW):
        r = await client.post("/api/oauth/revoke", data={"token": "nope"}, headers=_cf(CF_PEER))
        assert r.status_code == 200, r.text
    assert (
        await client.post("/api/oauth/revoke", data={"token": "n"}, headers=_cf(CF_PEER))
    ).status_code == 429
    assert (
        await client.post("/api/oauth/revoke", data={"token": "n"}, headers=_cf(OTHER_PEER))
    ).status_code == 200


async def test_device_authorization_is_rate_limited(client: httpx.AsyncClient) -> None:
    """This is what bounds unauthenticated row creation in ``oauth_device_codes``."""
    for _ in range(_DEVICE_AUTHZ_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/device_authorization",
            data={"client_id": CLIENT_ID},
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 200, r.text
    r = await client.post(
        "/api/oauth/device_authorization",
        data={"client_id": CLIENT_ID},
        headers=_cf(CF_PEER),
    )
    assert r.status_code == 429, r.text


async def test_device_authorization_limit_is_per_key(client: httpx.AsyncClient) -> None:
    for _ in range(_DEVICE_AUTHZ_MAX_PER_WINDOW):
        r = await client.post(
            "/api/oauth/device_authorization",
            data={"client_id": CLIENT_ID},
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 200, r.text
    assert (
        await client.post(
            "/api/oauth/device_authorization",
            data={"client_id": CLIENT_ID},
            headers=_cf(CF_PEER),
        )
    ).status_code == 429
    assert (
        await client.post(
            "/api/oauth/device_authorization",
            data={"client_id": CLIENT_ID},
            headers=_cf(OTHER_PEER),
        )
    ).status_code == 200


async def test_an_invalid_scope_does_not_consume_the_device_budget(
    client: httpx.AsyncClient,
) -> None:
    """Validation first, then the limit — a garbage probe fills no bucket."""
    for _ in range(_DEVICE_AUTHZ_MAX_PER_WINDOW + 5):
        r = await client.post(
            "/api/oauth/device_authorization",
            data={"client_id": CLIENT_ID, "scope": "root:owner"},
            headers=_cf(CF_PEER),
        )
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_scope"
    # The budget is untouched: a well-formed call still succeeds.
    r = await client.post(
        "/api/oauth/device_authorization",
        data={"client_id": CLIENT_ID},
        headers=_cf(CF_PEER),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# The public lookup route stays public — do not "fix" it
# ---------------------------------------------------------------------------


async def test_public_client_lookup_is_not_rate_limited(client: httpx.AsyncClient) -> None:
    """Its docstring defends being public; client_ids are high-entropy."""
    for _ in range(_ANON_DCR_MAX_PER_WINDOW * 3):
        r = await client.get("/api/oauth/clients/by-client-id/dcr-nope", headers=_cf(CF_PEER))
        assert r.status_code == 404, r.text
