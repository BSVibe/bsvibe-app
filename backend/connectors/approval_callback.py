"""Connector-agnostic interactive-approval handler — approve/reject from a tap.

A founder taps Approve / Reject on a "작업 완료" (shipped) deliverable card in a
chat connector (Telegram today; Slack / Discord next); this settles the held
Safe-Mode item straight from the connector — no PWA round trip. It is a
SYNCHRONOUS approve action, NOT a new run, so it deliberately stays OUT of the
intake pipeline (no WebhookReceiver / TriggerEvent); the public webhook route
(:mod:`backend.api.webhooks`) delegates here when a connector delivery carries an
interaction (the webhook_token + connector signature already gated the request
via the normal parser path — so the transport is trusted before we get here).

This module owns the CONNECTOR-NEUTRAL flow — resolve the held item, approve/deny
+ dispatch delivery, record the audit actor, edit the card to read like history,
best-effort ack — and reaches each connector ONLY through
:meth:`PluginRunner.dispatch_action` (the sanctioned backend→plugin seam). It has
NO ``plugin.*`` import.

The adapter contract — what a connector must provide to plug in
--------------------------------------------------------------
Each connector supplies one :class:`ApprovalConnectorAdapter` describing its
connector-specific surface; the generic :func:`handle_approval_callback` owns the
rest. A NEW connector (Slack / Discord) needs only:

1. A notify-builder button function (NOT part of this handler) that renders the
   card's Approve / Reject buttons carrying ``"<verb>:<deliverable_id>"`` where
   ``verb`` ∈ {``apv``, ``rej``} — the shared verb vocabulary this handler acts on.
2. Three ``@p.action``s in the plugin, dispatched via the runner:
   * ``parse_action`` — pure parse of the inbound body → a normalized dict
     ``{verb, deliverable_id, malformed, <ack-token field>, <message-ref fields>,
     <connector auth fields>}``. ``verb`` / ``deliverable_id`` are ``None`` and
     ``malformed`` is ``True`` for an unrecognised payload.
   * ``ack_action`` — acknowledge the tap (clear the spinner / ephemeral reply).
   * ``update_action`` — edit the card in place (keep body, append status, drop
     buttons).
3. Four adapter callables:
   * ``is_interaction(body)`` — is this inbound body an approve/reject tap we
     handle (vs a handshake / normal event the route falls through on)?
   * ``is_authorized(parsed, account)`` — the founder-auth decision (BEFORE any
     state change).
   * ``build_ack(parsed, text)`` — kwargs for ``ack_action`` (``None`` = nothing
     to ack).
   * ``build_update(parsed, status)`` — kwargs for ``update_action`` (``None`` =
     nothing to edit); this is where the connector keeps the original body,
     appends ``status``, and drops its buttons.

Behaviour this handler guarantees (do NOT weaken):
* AUTH FIRST — an unauthorized tap acks a localized "권한이 없어요" and stops
  before any state change.
* Approval is IRREVERSIBLE — a transient dispatch failure never reverts it.
* Cross-tenant scoping — a crafted deliverable_id resolves to no pending item in
  the account's workspace → treated as already-handled.
* Idempotent double-tap — a second tap on a settled item only acks (no re-edit,
  which would stack a second status line onto the already-appended card).
* Best-effort ack / update — after the state change commits, a failed UI call
  must NEVER propagate (a 500 would make the connector retry the callback).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
}


@dataclass(frozen=True)
class ApprovalConnectorAdapter:
    """The connector-specific surface :func:`handle_approval_callback` needs.

    ``connector`` names the connector (for logs). ``credential_key`` is the key the
    decrypted signing secret is placed under in the plugin context credentials
    (telegram: ``"bot_token"``). ``parse_action`` / ``ack_action`` /
    ``update_action`` are the plugin ``@p.action`` names. The four callables carry
    the connector-specific decisions — see the module docstring's adapter contract.
    """

    connector: str
    credential_key: str
    parse_action: str
    ack_action: str
    update_action: str
    is_interaction: Callable[[dict[str, Any]], bool]
    is_authorized: Callable[[dict[str, Any], ConnectorAccountRow], bool]
    build_ack: Callable[[dict[str, Any], str], dict[str, Any] | None]
    build_update: Callable[[dict[str, Any], str], dict[str, Any] | None]


def _t(key: str, language: str) -> str:
    variants = _STRINGS[key]
    return variants.get(language, variants["en"])


async def handle_approval_callback(  # noqa: PLR0911 — each return is one security guard
    *,
    adapter: ApprovalConnectorAdapter,
    raw_body: bytes,
    account: ConnectorAccountRow,
    session: AsyncSession,
    plugin: PluginMeta,
    cipher: CredentialCipher,
    dispatcher: Any | None = None,
    runner: PluginRunner | None = None,
) -> bool:
    """Handle one interactive approve/reject tap for ``adapter``'s connector.

    Returns ``True`` when the body was an interaction and was handled (the route
    replies 200), ``False`` when the body is NOT an interaction (the route falls
    through to its normal handshake/skip handling).
    """
    body = _load_json(raw_body)
    if not isinstance(body, dict) or not adapter.is_interaction(body):
        return False

    runner = runner or PluginRunner()
    context = _build_context(
        credentials={adapter.credential_key: cipher.decrypt(account.signing_secret_ciphertext)},
        config=dict(account.delivery_config),
    )
    language = await load_workspace_language(session, account.workspace_id)

    parsed: dict[str, Any] = await runner.dispatch_action(
        plugin, action_name=adapter.parse_action, context=context, kwargs={"body": body}
    )

    # 1) AUTH FIRST — the tap is honoured only for the authorized founder. Failing
    # stops here, before any state change.
    if not adapter.is_authorized(parsed, account):
        logger.info(
            "approval_callback_unauthorized",
            connector=adapter.connector,
            workspace_id=str(account.workspace_id),
        )
        await _ack(runner, plugin, adapter, context, parsed, _t("no_permission", language))
        return True

    # 2) Malformed data (unknown verb / no deliverable id) → friendly error.
    verb = parsed.get("verb")
    deliverable_raw = parsed.get("deliverable_id")
    if parsed.get("malformed") or not verb or not deliverable_raw:
        await _ack(runner, plugin, adapter, context, parsed, _t("bad_request", language))
        return True
    try:
        deliverable_id = uuid.UUID(str(deliverable_raw))
    except ValueError:
        await _ack(runner, plugin, adapter, context, parsed, _t("bad_request", language))
        return True

    # 3) Resolve the PENDING held item SCOPED to the account's workspace. A
    # crafted cross-workspace id finds nothing → treat as already-handled
    # (idempotent double-tap lands here too).
    item_id = await _pending_item_for(session, deliverable_id, account.workspace_id)
    if item_id is None:
        # Already-handled (idempotent double-tap, or a cross-workspace id that
        # resolves to nothing): the card was already edited on the FIRST tap, so
        # only ack the toast — re-editing would stack a second status line onto an
        # already-appended card.
        await _ack(runner, plugin, adapter, context, parsed, _t("already", language))
        return True

    # 4) Actor for the audit trail = the workspace owner.
    actor_id = await _owner_user_id(session, account.workspace_id)
    if actor_id is None:  # pragma: no cover - a workspace always has an owner
        logger.warning(
            "approval_callback_no_owner",
            connector=adapter.connector,
            workspace_id=str(account.workspace_id),
        )
        await _ack(runner, plugin, adapter, context, parsed, _t("bad_request", language))
        return True

    queue = SafeModeQueue(session)
    if verb == "apv":
        await _approve(session, queue, account, item_id, deliverable_id, actor_id, dispatcher)
        await _ack(runner, plugin, adapter, context, parsed, _t("approved_answer", language))
        await _update(runner, plugin, adapter, context, parsed, _t("approved_result", language))
    else:  # "rej"
        await queue.deny(
            workspace_id=account.workspace_id,
            item_id=item_id,
            actor_id=actor_id,
            reason=f"declined via {adapter.connector}",
        )
        await session.commit()
        await _ack(runner, plugin, adapter, context, parsed, _t("declined_answer", language))
        await _update(runner, plugin, adapter, context, parsed, _t("declined_result", language))
    return True


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
    # so ``backend.api.webhooks`` (which reaches this handler) stays free of any
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
            "approval_callback_dispatch_failed",
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


async def _ack(
    runner: PluginRunner,
    plugin: PluginMeta,
    adapter: ApprovalConnectorAdapter,
    context: Any,
    parsed: dict[str, Any],
    text: str,
) -> None:
    """Acknowledge the tap (clear the connector's spinner / ephemeral reply).

    Best-effort: the approve/deny already committed, so a failed UI ack must NEVER
    propagate (it would 500 the webhook → the connector retries the callback).
    ``build_ack`` returns ``None`` when there is nothing to ack (no token)."""
    kwargs = adapter.build_ack(parsed, text)
    if kwargs is None:  # pragma: no cover - a real tap always carries an ack token
        return
    try:
        await runner.dispatch_action(
            plugin, action_name=adapter.ack_action, context=context, kwargs=kwargs
        )
    except Exception:  # noqa: BLE001 — cosmetic ack; never fail the settled callback
        logger.warning("approval_callback_ack_failed", exc_info=True)


async def _update(
    runner: PluginRunner,
    plugin: PluginMeta,
    adapter: ApprovalConnectorAdapter,
    context: Any,
    parsed: dict[str, Any],
    status: str,
) -> None:
    """Edit the card to read like HISTORY — the connector's ``build_update`` keeps
    the original body, appends the approve/reject ``status`` line, and drops the
    buttons. ``None`` kwargs → no-op (nothing to edit).

    Best-effort (see :func:`_ack`): a failed edit must not fail the settled
    callback."""
    kwargs = adapter.build_update(parsed, status)
    if kwargs is None:  # pragma: no cover - a real card carries chat/message ids
        return
    try:
        await runner.dispatch_action(
            plugin, action_name=adapter.update_action, context=context, kwargs=kwargs
        )
    except Exception:  # noqa: BLE001 — cosmetic edit; never fail the settled callback
        logger.warning("approval_callback_update_failed", exc_info=True)


def _load_json(raw_body: bytes) -> Any:
    try:
        return json.loads(raw_body)
    except (ValueError, TypeError):
        return None


__all__ = [
    "ApprovalConnectorAdapter",
    "handle_approval_callback",
]
