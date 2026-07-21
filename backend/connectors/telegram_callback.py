"""Inbound Telegram callback_query handler — approve/reject from an inline tap.

A founder taps Approve / Reject on the "작업 완료" (shipped) card; this settles the
held Safe-Mode item straight from Telegram — no PWA round trip. It is a
SYNCHRONOUS approve action, NOT a new run, so it deliberately stays OUT of the
intake pipeline (no WebhookReceiver / TriggerEvent); the public webhook route
(:mod:`backend.api.webhooks`) delegates here when a telegram delivery carries a
``callback_query`` (the webhook_token + secret-token header already gated the
request via the normal parser path — so the transport is trusted before we get
here).

Boundary discipline (Lift Q3 / R2c): the route + this module never import
``plugin.telegram``. ALL telegram-specific work — parsing the callback_query and
the ``answerCallbackQuery`` / ``editMessageText`` Bot-API calls — lives in the
telegram plugin and is reached through :class:`PluginRunner` +
:class:`PluginMeta` (the same sanctioned backend→plugin dispatch seam the notify
push sender uses). The founder-auth decision + Safe-Mode approve/deny + delivery
dispatch are backend concerns and live here.

Security (do the auth BEFORE any state change): a tap is honoured only when the
chat is PRIVATE and ``callback_query.from.id`` equals the account's bound
``delivery_config['chat_id']`` (the founder's 1:1 chat). Either failing → answer
a localized "권한이 없어요" and stop. A crafted cross-workspace deliverable_id
resolves to no pending item in the ACCOUNT's workspace (scoped) → treated as
already-handled. All paths answer the callback (clear Telegram's spinner) and
return control to the route, which replies 200.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.identity.db import MembershipRow
from backend.identity.workspaces_db import load_workspace_language
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch._context import _build_context
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow, SafeModeStatus

logger = structlog.get_logger(__name__)

# Localized strings for the founder-facing acks + result lines. ko = 해요체, terse.
_STRINGS: dict[str, dict[str, str]] = {
    "no_permission": {"ko": "권한이 없어요.", "en": "You're not authorized to approve this."},
    "bad_request": {"ko": "요청을 처리할 수 없어요.", "en": "Couldn't process that request."},
    "already": {"ko": "이미 처리됐어요.", "en": "Already handled."},
    "approved_answer": {"ko": "승인했어요.", "en": "Approved."},
    "declined_answer": {"ko": "거절했어요.", "en": "Declined."},
    "approved_result": {"ko": "✅ 승인됨 — 내보냈어요.", "en": "✅ Approved — sent."},
    "declined_result": {"ko": "❌ 거절했어요.", "en": "❌ Declined."},
    "already_result": {"ko": "이미 처리됐어요.", "en": "Already handled."},
}

# editMessageText payload that DROPS the card's inline buttons (empty keyboard).
_NO_KEYBOARD: dict[str, Any] = {"inline_keyboard": []}


def _t(key: str, language: str) -> str:
    variants = _STRINGS[key]
    return variants.get(language, variants["en"])


async def handle_telegram_callback(  # noqa: PLR0911 — each return is one security guard
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    telegram: PluginMeta,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    runner: PluginRunner | None = None,
) -> bool:
    """Handle one telegram ``callback_query`` approve/reject tap.

    Returns ``True`` when the body was a callback_query and was handled (the
    route replies 200), ``False`` when the body is NOT a callback_query (the
    route falls through to its normal handshake/skip handling).
    """
    body = _load_json(raw_body)
    if not isinstance(body, dict) or "callback_query" not in body:
        return False

    runner = runner or PluginRunner()
    context = _build_context(
        credentials={"bot_token": cipher.decrypt(account.signing_secret_ciphertext)},
        config=dict(account.delivery_config),
    )
    language = await load_workspace_language(session, account.workspace_id)

    parsed: dict[str, Any] = await runner.dispatch_action(
        telegram, action_name="parse_callback", context=context, kwargs={"body": body}
    )
    callback_query_id = parsed.get("callback_query_id")

    # 1) AUTH FIRST — private chat AND the tapper IS the bound founder. Either
    # failing stops here, before any state change.
    if not _is_authorized_founder(parsed, account):
        logger.info(
            "telegram_callback_unauthorized",
            workspace_id=str(account.workspace_id),
            chat_type=parsed.get("chat_type"),
        )
        await _answer(runner, telegram, context, callback_query_id, _t("no_permission", language))
        return True

    # 2) Malformed data (unknown verb / no deliverable id) → friendly error.
    verb = parsed.get("verb")
    deliverable_raw = parsed.get("deliverable_id")
    if parsed.get("malformed") or not verb or not deliverable_raw:
        await _answer(runner, telegram, context, callback_query_id, _t("bad_request", language))
        return True
    try:
        deliverable_id = uuid.UUID(str(deliverable_raw))
    except ValueError:
        await _answer(runner, telegram, context, callback_query_id, _t("bad_request", language))
        return True

    # 3) Resolve the PENDING held item SCOPED to the account's workspace. A
    # crafted cross-workspace id finds nothing → treat as already-handled
    # (idempotent double-tap lands here too).
    item_id = await _pending_item_for(session, deliverable_id, account.workspace_id)
    if item_id is None:
        await _answer(runner, telegram, context, callback_query_id, _t("already", language))
        await _edit(
            runner,
            telegram,
            context,
            parsed,
            text=_t("already_result", language),
        )
        return True

    # 4) Actor for the audit trail = the workspace owner.
    actor_id = await _owner_user_id(session, account.workspace_id)
    if actor_id is None:  # pragma: no cover - a workspace always has an owner
        logger.warning("telegram_callback_no_owner", workspace_id=str(account.workspace_id))
        await _answer(runner, telegram, context, callback_query_id, _t("bad_request", language))
        return True

    queue = SafeModeQueue(session)
    if verb == "apv":
        await _approve(session, queue, account, item_id, deliverable_id, actor_id, dispatcher)
        await _answer(runner, telegram, context, callback_query_id, _t("approved_answer", language))
        await _edit(runner, telegram, context, parsed, text=_t("approved_result", language))
    else:  # "rej"
        await queue.deny(
            workspace_id=account.workspace_id,
            item_id=item_id,
            actor_id=actor_id,
            reason="declined via Telegram",
        )
        await session.commit()
        await _answer(runner, telegram, context, callback_query_id, _t("declined_answer", language))
        await _edit(runner, telegram, context, parsed, text=_t("declined_result", language))
    return True


def _is_authorized_founder(parsed: dict[str, Any], account: ConnectorAccountRow) -> bool:
    """The tap is the authorized founder iff the chat is PRIVATE and the tapper's
    ``from.id`` equals the account's bound ``chat_id`` (compared as strings, since
    ``delivery_config`` may store either an int or a str)."""
    if parsed.get("chat_type") != "private":
        return False
    bound_chat_id = account.delivery_config.get("chat_id")
    if bound_chat_id is None:
        return False
    return str(parsed.get("from_id")) == str(bound_chat_id)


async def _approve(
    session: AsyncSession,
    queue: SafeModeQueue,
    account: ConnectorAccountRow,
    item_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    actor_id: uuid.UUID,
    dispatcher: Any | None,
) -> None:
    """Flip pending→approved AND dispatch the delivery — mirroring the REST
    approve so there is one outbound code path. Approval is irreversible: a
    transient dispatch failure does NOT revert the approve (it surfaces in logs +
    on the next worker tick)."""
    ok = await queue.approve(workspace_id=account.workspace_id, item_id=item_id, actor_id=actor_id)
    await session.commit()
    if not ok:  # lost a race — no longer pending; nothing to dispatch
        return
    # Local imports keep the heavy delivery graph off this module's import chain,
    # so ``backend.api.webhooks`` (which imports this handler) stays free of any
    # transitive ``plugin`` edge (the R2c inbound-layer contract).
    from backend.workflow.application.runtime.delivery_runtime import (  # noqa: PLC0415
        build_delivery_adapter,
    )
    from backend.workflow.infrastructure.db import Deliverable  # noqa: PLC0415
    from backend.workflow.infrastructure.workers.delivery_worker import (  # noqa: PLC0415
        dispatch_delivery,
        persist_compensation_handles,
    )

    deliverable = await session.get(Deliverable, deliverable_id)
    artifact_type = (
        deliverable.deliverable_type.value if deliverable is not None else "direct_output"
    )

    if dispatcher is None:
        from backend.api.deps import _get_session_factory  # noqa: PLC0415

        dispatcher = await build_delivery_adapter(session_factory=_get_session_factory())

    try:
        result = await dispatch_delivery(
            dispatcher,
            workspace_id=account.workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
        )
        await persist_compensation_handles(session, deliverable_id=deliverable_id, result=result)
    except Exception:  # noqa: BLE001 — irreversible approve; never revert on a dispatch hiccup
        logger.warning(
            "telegram_callback_dispatch_failed",
            workspace_id=str(account.workspace_id),
            deliverable_id=str(deliverable_id),
            exc_info=True,
        )


async def _pending_item_for(
    session: AsyncSession, deliverable_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """The PENDING Safe-Mode item for this deliverable in THIS workspace (scoped —
    a cross-tenant deliverable_id finds nothing). ``None`` when nothing is held
    (already approved/denied/expired, or another workspace's item)."""
    stmt = (
        select(SafeModeQueueItemRow.id)
        .where(
            SafeModeQueueItemRow.deliverable_id == deliverable_id,
            SafeModeQueueItemRow.workspace_id == workspace_id,
            SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _owner_user_id(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID | None:
    """The workspace owner's ``user_id`` (an active ``role='owner'`` membership)."""
    stmt = (
        select(MembershipRow.user_id)
        .where(
            MembershipRow.workspace_id == workspace_id,
            MembershipRow.role == "owner",
            MembershipRow.left_at.is_(None),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _answer(
    runner: PluginRunner,
    telegram: PluginMeta,
    context: Any,
    callback_query_id: Any,
    text: str,
) -> None:
    if callback_query_id is None:  # pragma: no cover - always present on a real tap
        return
    await runner.dispatch_action(
        telegram,
        action_name="answer_callback_query",
        context=context,
        kwargs={"callback_query_id": callback_query_id, "text": text},
    )


async def _edit(
    runner: PluginRunner,
    telegram: PluginMeta,
    context: Any,
    parsed: dict[str, Any],
    *,
    text: str,
) -> None:
    """Replace the card's text with the result line and DROP the buttons, closing
    the loop visually. Missing chat/message ids → no-op (nothing to edit)."""
    chat_id = parsed.get("chat_id")
    message_id = parsed.get("message_id")
    if chat_id is None or message_id is None:  # pragma: no cover
        return
    await runner.dispatch_action(
        telegram,
        action_name="edit_message_text",
        context=context,
        kwargs={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": _NO_KEYBOARD,
        },
    )


def _load_json(raw_body: bytes) -> Any:
    try:
        return json.loads(raw_body)
    except (ValueError, TypeError):
        return None


@lru_cache(maxsize=1)
def _load_telegram_meta() -> PluginMeta | None:
    """The loaded telegram :class:`PluginMeta` (cached). ``None`` if the plugin is
    absent. Loads via importlib at call time — no static ``plugin.telegram`` edge
    (so ``backend.api.webhooks`` stays free of the reverse coupling)."""
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415

    plugins_dir = Path(__file__).resolve().parents[2] / "plugin"
    registry = PluginLoader(plugins_dir).load_all_sync()
    return registry.get("telegram")


async def process_telegram_callback(
    *,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    cipher: CredentialCipher,
) -> bool:
    """Route-facing entrypoint: load the telegram plugin + delegate to
    :func:`handle_telegram_callback` with the production dispatcher. Returns
    ``False`` (route falls through) when the body is not a callback_query or the
    telegram plugin is unavailable."""
    telegram = _load_telegram_meta()
    if telegram is None:  # pragma: no cover - telegram is always loaded in prod
        return False
    return await handle_telegram_callback(
        raw_body=raw_body,
        account=account,
        session=session,
        telegram=telegram,
        cipher=cipher,
    )


__all__ = ["handle_telegram_callback", "process_telegram_callback"]
