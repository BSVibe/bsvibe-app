"""The GitHub App's webhook must land — it had nowhere to go.

Measured 2026-09-30 (prod, read-only): the App manifest registers its hook at
``{issuer}/api/webhooks/github``, but the only webhook route the app mounts is
``/api/webhooks/{connector}/{webhook_token}``. Every App delivery 404'd. The
manifest also subscribed to ``push`` / ``pull_request`` only, so an issue never
left GitHub in the first place. ``trigger_events`` holds no github row after
2026-07-01 — an issue opened on a bound repo started nothing.

An App delivery carries no per-account token, so the route resolves it the way
the founder stated intent: the App's own webhook secret authenticates the
delivery, and the repo's Product × Connector BINDING names the workspace and
product. No binding → no run. Guessing a product from ``repo_url`` would route
a stranger's repo into whichever workspace happens to share the name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher, get_webhook_parser_registry
from backend.connectors.auth.app_credentials import upsert_app_credentials
from backend.connectors.auth.github_manifest import build_manifest
from backend.connectors.db import ConnectorAccountRow
from backend.data.rls import workspace_session_scope
from backend.extensions.plugin.webhook_registry import WebhookParserRegistry
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher
from backend.shared.wire_kinds import (
    PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID,
    PAYLOAD_KEY_RESOURCE_ID,
)
from backend.workflow.infrastructure.intake.db import TriggerEventRow
from plugin.github.webhook import parse_webhook

from .._support import db_engine

TEST_KEY = b"0123456789abcdef0123456789abcdef"
APP_SECRET = "github-app-webhook-secret"
REPO = "BSVibe/bsvibe-app"


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
    registry.register("github", parse_webhook)

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    app.dependency_overrides[get_webhook_parser_registry] = lambda: registry

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_app(sf: Any, cipher: CredentialCipher, *, webhook_secret: str | None) -> None:
    async with sf() as s:
        await upsert_app_credentials(
            s,
            provider="github",
            app_id="3980170",
            client_id="Iv1.test",
            client_secret="client-secret",
            private_key_pem="pem",
            app_slug="bsvibe",
            webhook_secret=webhook_secret,
            html_url=None,
            cipher=cipher,
        )
        await s.commit()


async def _seed_bound_repo(
    sf: Any,
    cipher: CredentialCipher,
    *,
    resource_id: str = REPO,
    is_active: bool = True,
    installation_id: int | None = 12345,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A workspace whose product is bound to ``resource_id`` through an active
    github connector account. Returns ``(workspace, product, account)`` ids."""
    ws = uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="WS", language="ko"))
        # Flush each FK LEVEL before the rows that point at it — SQLite's lax FK
        # enforcement hides a mis-ordered insert until PostgreSQL runs it.
        await s.flush()
        product = ProductRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            name="BSVibe",
            slug=f"p-{uuid.uuid4().hex[:8]}",
        )
        account = ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            connector="github",
            webhook_token="wht_" + uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("pat-token"),
            # 설치 id 는 설치 claim 이 서버에서 external_ref 에 기록한 값이다.
            external_ref=str(installation_id) if installation_id else None,
            delivery_config={"repo": resource_id, "base_branch": "main"},
            is_active=is_active,
        )
        s.add(product)
        s.add(account)
        await s.flush()
        s.add(
            ResourceBindingRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                product_id=product.id,
                connector_account_id=account.id,
                resource_id=resource_id,
                trigger={"filters": {}},
                selection={"repo": resource_id},
            )
        )
        await s.commit()
        return ws, product.id, account.id


def _issue_body(*, repo: str = REPO, title: str = "Fix the docs link") -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "issue": {"number": 1, "title": title, "body": "It 404s."},
            "repository": {"full_name": repo},
            "sender": {"login": "blas1n", "type": "User"},
            "installation": {"id": 12345},
        }
    ).encode()


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _post(
    client: httpx.AsyncClient,
    body: bytes,
    *,
    signature: str | None = None,
    delivery: str | None = None,
    event: str = "issues",
) -> httpx.Response:
    return await client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": signature if signature is not None else _sign(body),
            "Content-Type": "application/json",
        },
    )


async def _rows(sf: Any) -> list[TriggerEventRow]:
    async with sf() as s:
        return list((await s.execute(select(TriggerEventRow))).scalars().all())


# ── the route exists and lands a trigger on the bound product ────────────────


async def test_an_issue_on_a_bound_repo_lands_a_trigger_on_that_product(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    ws, product_id, account_id = await _seed_bound_repo(sf, cipher)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202, resp.text
    rows = await _rows(sf)
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == ws
    assert row.product_id == product_id
    assert row.source == "github"
    assert row.payload["intent_text"] == "Fix the docs link\n\nIt 404s."
    # The Receive stage finds the binding off these — the #922 seam.
    assert row.payload[PAYLOAD_KEY_CONNECTOR_ACCOUNT_ID] == str(account_id)
    assert row.payload[PAYLOAD_KEY_RESOURCE_ID] == REPO


async def test_a_redelivery_collapses_to_one_trigger(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)
    body = _issue_body()
    delivery = str(uuid.uuid4())

    first = await _post(client, body, delivery=delivery)
    second = await _post(client, body, delivery=delivery)

    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is True
    assert len(await _rows(sf)) == 1


async def test_every_workspace_that_bound_the_repo_gets_its_own_trigger(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    """One App serves every workspace; each binding is its own stated intent."""
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    ws_a, _, _ = await _seed_bound_repo(sf, cipher)
    ws_b, _, _ = await _seed_bound_repo(sf, cipher)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202, resp.text
    assert sorted(str(r.workspace_id) for r in await _rows(sf)) == sorted([str(ws_a), str(ws_b)])


# ── what must NOT land ───────────────────────────────────────────────────────


async def test_a_forged_delivery_is_rejected_and_lands_nothing(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)
    body = _issue_body()

    resp = await _post(client, body, signature=_sign(body, "not-the-app-secret"))

    assert resp.status_code == 401
    assert await _rows(sf) == []


async def test_an_unsigned_delivery_is_rejected(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)

    resp = await _post(client, _issue_body(), signature="")

    assert resp.status_code == 401
    assert await _rows(sf) == []


@pytest.mark.parametrize("webhook_secret", [None, "__no_app__"])
async def test_without_an_app_webhook_secret_the_route_is_not_found(
    sf: Any, client: Any, cipher: CredentialCipher, webhook_secret: str | None
) -> None:
    """No secret means nothing can be verified — never accept unverified."""
    if webhook_secret is None:
        await _seed_app(sf, cipher, webhook_secret=None)
    await _seed_bound_repo(sf, cipher)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 404
    assert await _rows(sf) == []


async def test_an_unbound_repo_is_skipped_not_guessed(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)

    resp = await _post(client, _issue_body(repo="someone/else"))

    assert resp.status_code == 202
    assert resp.json()["skipped"] is True
    assert await _rows(sf) == []


async def test_a_binding_on_an_inactive_account_is_ignored(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher, is_active=False)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202
    assert await _rows(sf) == []


async def test_an_uninteresting_event_is_skipped(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)

    resp = await _post(client, b'{"zen": "hi"}', event="ping")

    assert resp.status_code == 202
    assert await _rows(sf) == []


# ── BSVibe's own PRs do not come back as work ─────────────────────────────────


async def test_a_pr_bsvibe_opened_does_not_land_a_trigger(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    """BSVibe opens PRs with the founder's token, so the sender is a USER. 2026-09-30:
    PR #1100 came back as ``pull_request/opened`` and opened a run on itself. The
    branch comes from the delivery side's own ``run_branch_name`` so the parser's
    marker cannot drift from what is really pushed."""
    from backend.workflow.application.delivery.connector_dispatch._github import (
        run_branch_name,
    )

    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher)
    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": 1100,
                "title": "t",
                "head": {"ref": run_branch_name(uuid.uuid4())},
            },
            "repository": {"full_name": REPO},
            "sender": {"login": "blas1n", "type": "User"},
        }
    ).encode()

    resp = await _post(client, body, event="pull_request")

    assert resp.status_code == 202
    assert await _rows(sf) == []


# ── the App asks GitHub for what the route consumes ──────────────────────────


def test_the_app_manifest_subscribes_to_issue_events_it_can_read() -> None:
    manifest = build_manifest(
        homepage_url="https://x",
        redirect_url="https://x/r",
        oauth_callback_url="https://x/cb",
        webhook_url="https://x/api/webhooks/github",
    )
    assert {"issues", "issue_comment", "pull_request"} <= set(manifest["default_events"])
    assert manifest["default_permissions"]["issues"] in {"read", "write"}


# ── 설치 id 대조: 저장소 이름만으로는 배분하지 않는다 ──────────────────────────


async def test_webhook_skips_other_installation(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    """같은 저장소 이름을 바인딩했어도 설치 id 가 다른 워크스페이스에는 가지 않는다."""
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher, installation_id=999)

    resp = await _post(client, _issue_body())  # installation.id == 12345

    assert resp.status_code == 202
    assert await _rows(sf) == []


async def test_webhook_skips_account_without_installation(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    await _seed_bound_repo(sf, cipher, installation_id=None)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202
    assert await _rows(sf) == []


async def test_webhook_delivers_same_installation_only(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    ws_ok, _, _ = await _seed_bound_repo(sf, cipher, installation_id=12345)
    await _seed_bound_repo(sf, cipher, installation_id=999)

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202
    rows = await _rows(sf)
    assert [r.workspace_id for r in rows] == [ws_ok]


async def test_installation_id_in_delivery_config_is_not_trusted(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    """사용자가 쓸 수 있는 delivery_config 의 installation_id 로는 앱 웹훅을 받지 못한다."""
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    _, _, account_id = await _seed_bound_repo(sf, cipher, installation_id=None)
    async with sf() as s:
        row = await s.get(ConnectorAccountRow, account_id)
        row.delivery_config = {**row.delivery_config, "installation_id": 12345}
        await s.commit()

    resp = await _post(client, _issue_body())

    assert resp.status_code == 202
    assert await _rows(sf) == []


async def test_pat_account_without_installation_still_gets_events_on_its_own_webhook(
    sf: Any, client: Any, cipher: CredentialCipher
) -> None:
    """설치 기록이 없는 PAT 계정(delivery_config 에 repo/base_branch 만, external_ref 없음)은
    앱 웹훅에서는 빠지지만 계정별 웹훅 경로로는 그대로 이벤트를 받는다."""
    await _seed_app(sf, cipher, webhook_secret=APP_SECRET)
    ws, _, account_id = await _seed_bound_repo(sf, cipher, installation_id=None)
    async with sf() as s:
        account = await s.get(ConnectorAccountRow, account_id)
        assert account.external_ref is None
        assert account.delivery_config == {"repo": REPO, "base_branch": "main"}
        token = account.webhook_token

    body = _issue_body()
    resp = await client.post(
        f"/api/webhooks/github/{token}",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "pat-delivery-1",
            "X-Hub-Signature-256": _sign(body, "pat-token"),
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 202, resp.text
    assert resp.json().get("skipped") is not True
    assert [r.workspace_id for r in await _rows(sf)] == [ws]
