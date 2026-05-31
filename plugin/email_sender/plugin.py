"""Email-sender connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.ResendClient`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later be invoked by handle.

Compensation (Workflow §9.1): a transactional email, once delivered, **cannot
be recalled or unsent** through any provider API. It is therefore declared
``compensation_tier="t4_irreversible"`` with ``compensation_supported=False``.
The paired ``@p.compensate`` handler is intentionally a notify-style no-op: it
records that no clean undo exists rather than attempting (and failing) a revert.
A correction/retraction is a *new* email the agent would author explicitly —
the framework should not silently model that as an automatic undo, so we do not
claim ``t3_new_artifact`` here.

This connector is **outbound-only** — there is no ``@p.inbound`` capability.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin.context import SkillContext
from bsvibe_sdk import plugin
from plugin.email_sender.client import DEFAULT_BASE_URL, ResendClient

p = plugin(
    name="email-sender",
    version="0.1.0",
    description="Email-sender connector — send transactional email from deliverables via Resend.",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "api_key",
            "description": "Resend API key (re_...) used as the Bearer token.",
            "required": True,
        },
        {
            "name": "from",
            "description": "Default verified sender address (e.g. 'BSVibe <noreply@bsvibe.dev>').",
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> ResendClient:
    """Build an authed client from the injected credentials.

    ``config['resend_api_url']`` overrides the API base. Raises ``ValueError``
    (→ ``PluginRunError`` at the runner boundary) when no api_key credential is
    present.
    """
    api_key = context.credentials.get("api_key")
    if not api_key:
        raise ValueError("email-sender: missing required 'api_key' credential")
    base_url = context.config.get("resend_api_url", DEFAULT_BASE_URL)
    return ResendClient(api_key, base_url=base_url)


def _sender(context: SkillContext, event: dict[str, Any]) -> str:
    """Resolve the ``from`` address: event > config > 'from' credential."""
    sender = (
        event.get("from") or context.config.get("email_from") or context.credentials.get("from")
    )
    if not sender:
        raise ValueError(
            "email-sender: missing 'from' (event) / 'email_from' (config) / 'from' credential"
        )
    return str(sender)


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["email"],
    compensation_tier="t4_irreversible",
    compensation_supported=False,
)
async def deliver_email(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Send an email from a deliverable.

    The ``event`` carries ``to`` / ``subject`` / ``body`` (mirroring how the
    github/notion outbounds read their event keys). ``body`` is sent as an HTML
    body unless ``event['as_text']`` is truthy.
    """
    to = event["to"]
    subject = event["subject"]
    body = event.get("body", "")
    as_text = bool(event.get("as_text"))
    sender = _sender(context, event)
    client = _client(context)
    if as_text:
        data = await client.send_email(sender=sender, to=to, subject=subject, text=body)
    else:
        data = await client.send_email(sender=sender, to=to, subject=subject, html=body)
    message_id = str(data["id"])
    return {
        "artifact_type": "email",
        "external_ref": f"resend://email/{message_id}",
        "url": None,
        "compensation_handle": {
            "kind": "email",
            "message_id": message_id,
            "to": to,
            "subject": subject,
        },
    }


# ── compensation (Workflow §9 — T4 irreversible, idempotent no-op) ─────────────


@p.compensate(artifact_types=["email"])
async def revert_email(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Record that a sent email cannot be undone (T4 — irreversible).

    A delivered transactional email cannot be recalled through the Resend API
    (or any provider), so this handler performs no remote call. It is
    idempotent — re-invocation returns the same uncompensable record — and
    surfaces a human-readable reason for the audit trail.
    """
    message_id = str(handle.get("message_id", "?"))
    return {
        "status": "uncompensable",
        "tier": "t4_irreversible",
        "already": True,
        "summary": (
            f"email {message_id} cannot be recalled; "
            "send a correction email if a retraction is needed"
        ),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="send_email",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string", "description": "recipient email address"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "HTML (or plain text) email body"},
        },
        "additionalProperties": False,
    },
)
async def send_email(
    context: SkillContext,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    sender = _sender(context, {})
    client = _client(context)
    data = await client.send_email(sender=sender, to=to, subject=subject, html=body)
    message_id = str(data["id"])
    return {
        "message_id": message_id,
        "external_ref": f"resend://email/{message_id}",
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the email-sender connector.

    Reads ``RESEND_API_KEY`` and the optional default sender ``RESEND_FROM``
    from the environment and persists them under the ``email-sender``
    namespace. Env-based ingestion keeps secrets out of shell history and
    process args (python-security) and stays non-interactive for CI / headless
    setup.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise ValueError("email-sender setup: set RESEND_API_KEY in the environment")
    data: dict[str, Any] = {"api_key": api_key}
    sender = os.environ.get("RESEND_FROM")
    if sender:
        data["from"] = sender
    await cred_store.store("email-sender", data)
    return data
