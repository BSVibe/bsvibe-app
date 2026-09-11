"""Registering telegram's inbound webhook — 게이트 3 후속.

Telegram is the one connector whose inbound route the product can wire ITSELF:
its Bot API exposes ``setWebhook``. Slack/Discord/GitHub are configured in their
own consoles, so they have no equivalent here.

**Why this module exists.** Asked directly on 2026-09-11, Telegram answered
``url_set: False`` for the prod bot — with no ``last_error_*`` fields at all.
Absent error fields are the decisive reading: Telegram had never *attempted* a
delivery, so nothing was blocking it. **No webhook was registered.** It had
worked once (a real ``webhook`` TriggerEvent on 2026-08-09) and was lost, and
``setWebhook`` appeared nowhere in the repo, so nothing could restore it.

The failure is asymmetric and silent: outbound is unaffected, so the founder
keeps receiving approve/deny cards whose buttons do nothing, because Telegram has
nowhere to deliver the ``callback_query``.

**The secret is not optional here.** :class:`~backend.connectors.resolver.ConnectorResolver`
verifies inbound updates against ``delivery_config["webhook_secret"]`` and falls
back, when absent, to the decrypted signing secret — which for telegram is the
BOT TOKEN. A bot token contains ``:``; Telegram's ``secret_token`` accepts only
``A-Za-z0-9_-``, so that value can never arrive in the header. Registering a URL
without minting a secret therefore produces a webhook whose every update fails
verification — a repair that looks done and changes nothing. So registration
mints the secret, stores it on the account, and hands Telegram the same value.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

#: The only characters Telegram accepts in ``setWebhook``'s ``secret_token``.
#: ``secrets.token_urlsafe`` draws from exactly this set, which is why it is the
#: generator below — and why a bot token (it contains ``:``) can never be one.
TELEGRAM_SECRET_ALPHABET = string.ascii_letters + string.digits + "_-"

#: Bytes of entropy per minted secret. The secret is the only thing between the
#: public ingress and a forged update, so this is sized like a credential, not
#: like a nonce.
_SECRET_BYTES = 32

#: Where the founder-visible secret lives. ``delivery_config`` rather than a new
#: column for the reason the resolver already documents: telegram needs a SECOND
#: auth value beside its signing secret, and freeform config is the established
#: home for that (trello-style) — no schema change.
WEBHOOK_SECRET_KEY = "webhook_secret"  # noqa: S105 — a dict key, not a secret


@runtime_checkable
class TelegramWebhookApi(Protocol):
    """The two Bot API calls this module needs."""

    async def set_webhook(self, url: str, *, secret_token: str) -> dict[str, Any]: ...

    async def get_webhook_info(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TelegramWebhookStatus:
    """What Telegram says about its own delivery target.

    ``registered`` and ``last_error`` are deliberately separate: "nobody told
    Telegram where to deliver" and "Telegram tried and was refused" are different
    faults with different fixes, and prod spent weeks in the first while the
    product could surface neither.
    """

    registered: bool
    pending_update_count: int = 0
    last_error: str | None = None


def telegram_webhook_url(public_base_url: str, webhook_token: str) -> str:
    """The ABSOLUTE ingress URL to hand Telegram.

    The MCP tools' ``_webhook_url`` returns a path (``/api/webhooks/…``), which a
    human can paste under an origin they know but which is useless as the value
    of ``setWebhook``. ``public_base_url`` is founder-set config, so a stray
    trailing slash must not become a double-slash path that no route matches.
    """
    return f"{public_base_url.rstrip('/')}/api/webhooks/telegram/{webhook_token}"


def mint_webhook_secret() -> str:
    """A fresh secret Telegram is willing to echo back."""
    return secrets.token_urlsafe(_SECRET_BYTES)


def _is_telegram_legal(secret: object) -> TypeGuard[str]:
    """Narrows to ``str``: the caller hands the result straight to ``set_webhook``,
    whose ``secret_token`` is typed ``str``."""
    return isinstance(secret, str) and bool(secret) and set(secret) <= set(TELEGRAM_SECRET_ALPHABET)


async def register_telegram_webhook(
    account: Any,
    *,
    telegram: TelegramWebhookApi,
    public_base_url: str,
) -> TelegramWebhookStatus:
    """Point Telegram at this account's ingress, minting a secret if needed.

    Mutates ``account.delivery_config`` in place with the secret actually
    registered; the caller owns the commit. The founder's other config keys are
    preserved — ``chat_id`` in particular is how outbound finds its way back.

    An existing LEGAL secret is reused: rotating on every repair would invalidate
    updates already in flight and make "did this change anything?" unanswerable.
    An existing ILLEGAL one (a bot token, say) is replaced rather than passed on,
    because Telegram would reject it and the header could never match.
    """
    config = dict(account.delivery_config or {})
    stored = config.get(WEBHOOK_SECRET_KEY)
    secret: str = stored if _is_telegram_legal(stored) else mint_webhook_secret()
    if secret != stored:
        logger.info(
            "telegram_webhook_secret_minted",
            connector_id=str(getattr(account, "id", "")),
            replaced_illegal=stored is not None,
        )
    config[WEBHOOK_SECRET_KEY] = secret
    # Reassign rather than mutate: ``delivery_config`` is a JSON column and
    # SQLAlchemy does not see in-place edits to a plain dict as dirty.
    account.delivery_config = config

    url = telegram_webhook_url(public_base_url, account.webhook_token)
    await telegram.set_webhook(url, secret_token=secret)
    logger.info(
        "telegram_webhook_registered",
        connector_id=str(getattr(account, "id", "")),
        # The token is a capability — log that we registered, never the URL.
        public_base_url=public_base_url,
    )
    return await telegram_webhook_status(telegram)


async def telegram_webhook_status(telegram: TelegramWebhookApi) -> TelegramWebhookStatus:
    """Ask Telegram what it currently believes about this bot's webhook."""
    info = await telegram.get_webhook_info()
    last_error = info.get("last_error_message")
    return TelegramWebhookStatus(
        registered=bool(info.get("url")),
        pending_update_count=int(info.get("pending_update_count") or 0),
        last_error=str(last_error) if last_error else None,
    )


__all__ = [
    "TELEGRAM_SECRET_ALPHABET",
    "WEBHOOK_SECRET_KEY",
    "TelegramWebhookApi",
    "TelegramWebhookStatus",
    "mint_webhook_secret",
    "register_telegram_webhook",
    "telegram_webhook_status",
    "telegram_webhook_url",
]
