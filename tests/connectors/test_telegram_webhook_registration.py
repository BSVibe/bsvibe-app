"""Telegram inbound must be REGISTERED, not merely routed — 게이트 3 후속.

Measured on prod 2026-09-11 by asking Telegram itself (``getWebhookInfo``)::

    url_set: False
    pending_update_count: 0
    (no last_error_date / last_error_message at all)

The absent error fields are the decisive part: Telegram had never *attempted* a
delivery, so this was never a Cloudflare block — **no webhook was registered**.
It had worked once (the single ``telegram`` TriggerEvent, 2026-08-09, is a real
``webhook`` receive) and was lost. ``setWebhook`` appears **nowhere** in this
repo, so nothing restores it: once lost, lost for good.

The failure is asymmetric and silent. Outbound (box → Telegram) is unaffected, so
the founder keeps receiving approve/deny cards — and pressing one does nothing,
because Telegram has nowhere to deliver the ``callback_query``. The connector row
advertises ``webhook_trigger: true`` and ``interactive_approval: true``: claims
the product cannot honour.

**Registering the URL alone would not have fixed it.** ``ConnectorResolver``
verifies inbound updates against ``delivery_config["webhook_secret"]`` and, when
that is absent, falls back to the decrypted signing secret — which for telegram
IS THE BOT TOKEN. A bot token contains ``:``, and Telegram's ``secret_token``
only accepts ``A-Za-z0-9_-``, so that value can never come back in the header.
Prod's connector has ``delivery_config = {"chat_id": …}`` — no secret. Wiring
``setWebhook`` without minting one would have registered a webhook whose every
update then failed verification: a fix that looks done and changes nothing.

So registration mints the secret, stores it, and hands Telegram the same value.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.connectors.telegram_webhook import (
    TELEGRAM_SECRET_ALPHABET,
    mint_webhook_secret,
    telegram_webhook_url,
)

# ── The URL we hand Telegram ─────────────────────────────────────────────────


def test_the_registered_url_is_absolute() -> None:
    """Telegram calls us; a relative path is unusable to it.

    ``_webhook_url`` in the MCP tools returns ``/api/webhooks/…`` — fine for a
    human to paste under an origin they know, useless as the value of
    ``setWebhook``.
    """
    url = telegram_webhook_url("https://api.bsvibe.dev", "tok123")
    assert url == "https://api.bsvibe.dev/api/webhooks/telegram/tok123"


def test_the_url_tolerates_a_trailing_slash_on_the_origin() -> None:
    """``oauth_issuer`` is founder-set config; a stray slash must not produce
    a double-slash path that no route matches."""
    assert (
        telegram_webhook_url("https://api.bsvibe.dev/", "tok123")
        == "https://api.bsvibe.dev/api/webhooks/telegram/tok123"
    )


# ── The secret Telegram is allowed to echo ───────────────────────────────────


def test_the_minted_secret_uses_only_telegram_legal_characters() -> None:
    """``A-Za-z0-9_-`` only — the constraint that rules the bot token out.

    Generated 200 times rather than once: a single sample passes by luck for an
    alphabet that is *mostly* legal.
    """
    for _ in range(200):
        secret = mint_webhook_secret()
        assert secret, "an empty secret would disable verification entirely"
        assert set(secret) <= set(TELEGRAM_SECRET_ALPHABET), secret
        assert ":" not in secret


def test_minted_secrets_are_unique_and_long_enough() -> None:
    """It is the only thing standing between the endpoint and a forged update."""
    secrets = {mint_webhook_secret() for _ in range(200)}
    assert len(secrets) == 200
    assert all(len(s) >= 32 for s in secrets)


def test_a_bot_token_is_not_a_usable_secret() -> None:
    """The negative control that names the trap.

    This is the value the resolver falls back to today. Pinning it here means a
    future edit that "simplifies" registration by reusing the signing secret
    fails loudly instead of silently registering an unverifiable webhook.
    """
    bot_token = "8242700007:AAHdE4kSomeThingLikeARealToken"
    assert not set(bot_token) <= set(TELEGRAM_SECRET_ALPHABET)


# ── Registration itself ──────────────────────────────────────────────────────


class _FakeTelegram:
    """Records what ``setWebhook`` was asked to register."""

    def __init__(self, *, info: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._info = info or {"url": "", "pending_update_count": 0}

    async def set_webhook(self, url: str, *, secret_token: str) -> dict[str, Any]:
        self.calls.append({"url": url, "secret_token": secret_token})
        self._info = {"url": url, "pending_update_count": 0}
        return {"ok": True}

    async def get_webhook_info(self) -> dict[str, Any]:
        return self._info


class _Account:
    """The shape ``ConnectorAccountRow`` presents to registration."""

    def __init__(self, delivery_config: dict[str, Any] | None = None) -> None:
        self.id = uuid.uuid4()
        self.connector = "telegram"
        self.webhook_token = "wh-token-abc"
        self.delivery_config: dict[str, Any] = delivery_config or {"chat_id": "123"}


@pytest.mark.asyncio
async def test_registration_mints_a_secret_when_the_account_has_none() -> None:
    """Prod's connector: ``delivery_config = {"chat_id": …}``."""
    from backend.connectors.telegram_webhook import register_telegram_webhook

    account = _Account()
    tg = _FakeTelegram()
    result = await register_telegram_webhook(
        account, telegram=tg, public_base_url="https://api.bsvibe.dev"
    )

    assert len(tg.calls) == 1
    call = tg.calls[0]
    assert call["url"] == "https://api.bsvibe.dev/api/webhooks/telegram/wh-token-abc"
    sent = call["secret_token"]
    # A real secret, not merely "the same nothing on both sides" — comparing the
    # two alone passes trivially when both are empty, which is exactly what a
    # naive "just call setWebhook" implementation produces.
    assert sent, "registration must MINT a secret, not register an empty one"
    assert set(sent) <= set(TELEGRAM_SECRET_ALPHABET)
    assert len(sent) >= 32
    # The value Telegram will echo MUST be the value the resolver verifies against.
    assert sent == account.delivery_config["webhook_secret"]
    assert result.registered is True
    # Minting must not clobber what the founder configured.
    assert account.delivery_config["chat_id"] == "123"


@pytest.mark.asyncio
async def test_registration_reuses_an_existing_secret() -> None:
    """Re-registering must not rotate the secret.

    A new secret on every repair would invalidate updates already in flight, and
    would make "did this change anything?" unanswerable.
    """
    from backend.connectors.telegram_webhook import register_telegram_webhook

    account = _Account({"chat_id": "123", "webhook_secret": "already-set-value"})
    tg = _FakeTelegram()
    await register_telegram_webhook(account, telegram=tg, public_base_url="https://api.bsvibe.dev")

    assert tg.calls[0]["secret_token"] == "already-set-value"
    assert account.delivery_config["webhook_secret"] == "already-set-value"


@pytest.mark.asyncio
async def test_registration_replaces_an_illegal_stored_secret() -> None:
    """A stored secret Telegram would reject is worse than none.

    ``setWebhook`` would fail (or, if it accepted it, the header could never
    match), so registration mints a legal replacement rather than passing it on.
    """
    from backend.connectors.telegram_webhook import register_telegram_webhook

    account = _Account({"chat_id": "123", "webhook_secret": "8242700007:AAHdE4k"})
    tg = _FakeTelegram()
    await register_telegram_webhook(account, telegram=tg, public_base_url="https://api.bsvibe.dev")

    sent = tg.calls[0]["secret_token"]
    assert ":" not in sent
    assert set(sent) <= set(TELEGRAM_SECRET_ALPHABET)
    assert account.delivery_config["webhook_secret"] == sent


@pytest.mark.asyncio
async def test_status_reports_an_unregistered_webhook() -> None:
    """The state prod was in, made visible instead of silent.

    ``url_set: False`` with no error fields is exactly "Telegram was never told
    where to deliver" — the reading that distinguishes this from a blocked
    delivery, and the one nothing in the product could previously surface.
    """
    from backend.connectors.telegram_webhook import telegram_webhook_status

    tg = _FakeTelegram(info={"url": "", "pending_update_count": 0})
    status = await telegram_webhook_status(tg)
    assert status.registered is False
    assert status.last_error is None


@pytest.mark.asyncio
async def test_status_surfaces_a_delivery_error() -> None:
    """A registered webhook that Telegram cannot reach is a DIFFERENT problem
    from an unregistered one, and must not read the same."""
    from backend.connectors.telegram_webhook import telegram_webhook_status

    tg = _FakeTelegram(
        info={
            "url": "https://api.bsvibe.dev/api/webhooks/telegram/x",
            "pending_update_count": 7,
            "last_error_date": 1788847817,
            "last_error_message": "Wrong response from the webhook: 403 Forbidden",
        }
    )
    status = await telegram_webhook_status(tg)
    assert status.registered is True
    assert status.last_error is not None
    assert "403" in status.last_error
    assert status.pending_update_count == 7
