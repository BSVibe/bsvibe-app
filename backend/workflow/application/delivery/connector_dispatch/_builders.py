"""Per-connector outbound event shaping (Lift §17.7).

One builder per connector turns ``{deliverable content} + {connector
delivery_config}`` into a :class:`ShapedEvent` (the dispatch-ready event dict +
the connector's ``artifact_type`` + which credential slot to inject the
decrypted account secret under).

Content (title/body) is sourced from the deliverable; routing / system fields
(e.g. notion's ``parent_page_id``) come from the stable founder-set
``delivery_config`` — never from LLM / work output
(no-LLM-output-for-system-fields rule).

The :data:`OUTBOUND_EVENT_BUILDERS` map IS the extensible seam: a connector with
no entry here has no v1 outbound event-shaping and is skipped (logged), not
errored. v1 ships notion + slack + email-sender + telegram + discord + linear
+ trello + sentry. github is a special case (needs git-ops, not a simple event
dict) — see :mod:`._github`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.workflow.domain.delivery import ArtifactType


@dataclass(slots=True)
class ShapedEvent:
    """The dispatch-ready outbound: which ``artifact_type`` + the event dict.

    ``credential_key`` is the credential slot the decrypted per-account secret
    is injected under for THIS connector's outbound. Connectors read their
    token from different keys (notion ``token``, slack ``bot_token``,
    email-sender ``api_key``); the builder declares which one so the adapter
    lands the single stored secret in the slot the plugin's ``_client`` reads.

    ``extra_credentials`` carries ADDITIONAL non-secret credential slots a
    connector needs alongside the single decrypted account secret. A
    ``connector_account`` stores exactly one encrypted secret
    (``signing_secret_ciphertext``), but a few connectors authenticate with two
    values — e.g. trello sends BOTH a ``key`` (its API key, an app-level public
    identifier) and a ``token`` (the user-authorizing secret) as query params.
    The genuinely secret half (trello ``token``) is the decrypted account secret
    under ``credential_key``; the non-secret half (trello ``api_key``) is sourced
    from the founder-set ``delivery_config`` and carried here so the adapter can
    inject both into ``context.credentials``. This avoids changing the
    single-secret ``connector_account`` schema. See :func:`build_trello_event`.
    """

    artifact_type: ArtifactType
    event: dict[str, Any]
    credential_key: str = "token"
    extra_credentials: dict[str, str] = field(default_factory=dict)


# A builder maps {deliverable content} + {connector delivery_config} → a
# ShapedEvent. Content (title/body) is sourced from the deliverable; routing /
# system fields (e.g. parent_page_id) from the stable config.
OutboundEventBuilder = Callable[[dict[str, Any], dict[str, Any]], ShapedEvent]


def _split_summary(summary: str) -> tuple[str, str]:
    """First non-empty line → title; the full summary → body.

    A deliverable summary is free-form text. The first line is the most
    title-like fragment; the whole summary is kept as the body so no content is
    dropped. Empty summary → a stable placeholder title (Notion rejects an
    empty title property).
    """
    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    title = lines[0] if lines else "Delivered artifact"
    return title, summary.strip()


def _summary_with_refs(content: dict[str, Any]) -> tuple[str, str]:
    """``(title, body)`` from the deliverable summary, with ``artifact_refs``
    appended to the body as a trailing reference list.

    Shared by the message-style builders (telegram/discord) and the
    issue/card-style builders (linear/trello): the title is the first non-empty
    summary line, the body is the whole summary plus a linked artifact list so no
    produced artifact is dropped from the delivered content.
    """
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    return title, body


def build_notion_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into notion's ``deliver_page`` event.

    * ``parent_page_id`` — routing, from the stable ``delivery_config``.
    * ``title`` — first line of the deliverable summary.
    * ``body`` — the deliverable summary, with any ``artifact_refs`` appended
      as a trailing reference list (so the delivered page links the produced
      artifacts).
    """
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    return ShapedEvent(
        artifact_type="page",
        event={
            "parent_page_id": delivery_config["parent_page_id"],
            "title": title,
            "body": body,
        },
    )


def build_slack_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into slack's ``deliver_message`` event.

    * ``channel`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``channel`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``), surfaced as a failed action rather than posting to a
      wrong / default channel.
    * ``text`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list so the message links the produced artifacts.

    ``artifact_type`` is ``slack_message`` (what slack's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    channel = delivery_config.get("channel")
    if not channel:
        raise ValueError("slack delivery_config missing required 'channel'")
    summary = str(content.get("summary") or "")
    _title, text = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        text = f"{text}\n\nArtifacts:\n{refs}" if text else f"Artifacts:\n{refs}"
    return ShapedEvent(
        artifact_type="slack_message",
        event={"channel": str(channel), "text": text},
        credential_key="bot_token",
    )


def build_email_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into email-sender's ``deliver_email`` event.

    * ``to`` — routing, from the stable ``delivery_config`` (never derived from
      the work text). A missing / empty ``to`` is a misconfigured delivery
      target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``from`` — optional founder-set sender override from ``delivery_config``;
      omitted when unset so the email-sender plugin falls back to its own
      ``email_from`` config / ``from`` credential.
    * ``subject`` — first non-empty line of the deliverable summary.
    * ``body`` — the deliverable summary (sent as plain text via ``as_text``),
      with any ``artifact_refs`` appended as a trailing reference list.

    ``artifact_type`` is ``email`` (what email-sender's ``@p.outbound``
    declares); the decrypted account secret is injected as ``api_key``.
    """
    to = delivery_config.get("to")
    if not to:
        raise ValueError("email delivery_config missing required 'to'")
    summary = str(content.get("summary") or "")
    subject, body = _split_summary(summary)
    artifact_refs = content.get("artifact_refs") or []
    if artifact_refs:
        refs = "\n".join(f"- {ref}" for ref in artifact_refs)
        body = f"{body}\n\nArtifacts:\n{refs}" if body else f"Artifacts:\n{refs}"
    event: dict[str, Any] = {
        "to": str(to),
        "subject": subject,
        "body": body,
        "as_text": True,
    }
    sender = delivery_config.get("from")
    if sender:
        event["from"] = str(sender)
    return ShapedEvent(
        artifact_type="email",
        event=event,
        credential_key="api_key",
    )


def build_telegram_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into telegram's ``deliver_message`` event.

    * ``chat_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``chat_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``text`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list.

    ``artifact_type`` is ``telegram_message`` (what telegram's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    chat_id = delivery_config.get("chat_id")
    if not chat_id:
        raise ValueError("telegram delivery_config missing required 'chat_id'")
    _title, text = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="telegram_message",
        event={"chat_id": str(chat_id), "text": text},
        credential_key="bot_token",
    )


def build_discord_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into discord's ``deliver_message`` event.

    * ``channel_id`` — routing, from the stable ``delivery_config`` (never
      derived from the work text). A missing / empty ``channel_id`` is a
      misconfigured delivery target → ``ValueError`` (mirrors notion raising on
      a missing ``parent_page_id``).
    * ``content`` — the deliverable summary, with any ``artifact_refs`` appended
      as a trailing reference list.

    ``artifact_type`` is ``discord_message`` (what discord's ``@p.outbound``
    declares); the decrypted account secret is injected as ``bot_token``.
    """
    channel_id = delivery_config.get("channel_id")
    if not channel_id:
        raise ValueError("discord delivery_config missing required 'channel_id'")
    _title, body = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="discord_message",
        event={"channel_id": str(channel_id), "content": body},
        credential_key="bot_token",
    )


def build_linear_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into linear's ``deliver_issue`` event.

    * ``team_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``team_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``). NOTE: the linear plugin also falls back to
      ``config['linear_team_id']``, but the builder explicitly sets the event
      ``team_id`` so the routing source is unambiguous and config-driven.
    * ``title`` — first non-empty line of the deliverable summary.
    * ``description`` — the deliverable summary, with any ``artifact_refs``
      appended as a trailing reference list.

    ``artifact_type`` is ``issue`` (what linear's ``@p.outbound`` declares); the
    decrypted account secret is injected as ``api_key``.
    """
    team_id = delivery_config.get("team_id")
    if not team_id:
        raise ValueError("linear delivery_config missing required 'team_id'")
    title, description = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="issue",
        event={"team_id": str(team_id), "title": title, "description": description},
        credential_key="api_key",
    )


def build_trello_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into trello's ``deliver_card`` event.

    * ``list_id`` — routing, from the stable ``delivery_config`` (never derived
      from the work text). A missing / empty ``list_id`` is a misconfigured
      delivery target → ``ValueError`` (mirrors notion raising on a missing
      ``parent_page_id``).
    * ``title`` — first non-empty line of the deliverable summary (the trello
      plugin maps the event ``title`` to the card ``name``).
    * ``desc`` — the deliverable summary, with any ``artifact_refs`` appended as
      a trailing reference list.

    **Dual-secret caveat.** Trello authenticates with TWO query-param values:
    a ``key`` (its API key — an app-level, non-user-secret identifier) and a
    ``token`` (the user-authorizing secret). A ``connector_account`` stores only
    ONE encrypted secret (``signing_secret_ciphertext``) — we use it for the
    genuinely secret half, the trello ``token`` (``credential_key="token"``).
    The non-secret ``api_key`` is sourced from the founder-set
    ``delivery_config['api_key']`` and carried in ``extra_credentials`` so the
    adapter injects both slots the trello ``_client`` reads — WITHOUT changing
    the single-secret ``connector_account`` schema. A missing config ``api_key``
    is a misconfigured target → ``ValueError`` (the trello client requires both).

    If trello ever needs the API key kept secret too, the proper fix is a richer
    multi-secret ``connector_account`` credential model (out of scope here).
    """
    list_id = delivery_config.get("list_id")
    if not list_id:
        raise ValueError("trello delivery_config missing required 'list_id'")
    api_key = delivery_config.get("api_key")
    if not api_key:
        raise ValueError("trello delivery_config missing required 'api_key'")
    title, desc = _summary_with_refs(content)
    return ShapedEvent(
        artifact_type="card",
        event={"list_id": str(list_id), "title": title, "desc": desc},
        credential_key="token",
        extra_credentials={"api_key": str(api_key)},
    )


def build_sentry_event(content: dict[str, Any], delivery_config: dict[str, Any]) -> ShapedEvent:
    """Shape a Deliverable into sentry's ``deliver_resolve`` event.

    * ``issue_id`` — routing / target, from the stable ``delivery_config``
      (never derived from the work text). A missing / empty ``issue_id`` is a
      misconfigured delivery target → ``ValueError`` (mirrors notion raising on
      a missing ``parent_page_id``).

    Sentry's ``@p.outbound`` (``deliver_resolve``) resolves an issue by id — it
    accepts ONLY ``issue_id`` (no title / body), so the deliverable ``content``
    is not mapped onto any event field (mapping it would invent fields the
    sentry outbound does not support).

    ``artifact_type`` is ``sentry_issue_update`` (what sentry's ``@p.outbound``
    declares); the decrypted account secret is injected as ``auth_token`` (the
    slot the sentry plugin's ``_client`` reads).
    """
    issue_id = delivery_config.get("issue_id")
    if not issue_id:
        raise ValueError("sentry delivery_config missing required 'issue_id'")
    return ShapedEvent(
        artifact_type="sentry_issue_update",
        event={"issue_id": str(issue_id)},
        credential_key="auth_token",
    )


# The extensible seam: a connector with no entry here has no v1 outbound
# event-shaping and is skipped (logged). This ships notion + slack +
# email-sender + telegram + discord + linear + trello + sentry; github (needs a
# git-ops layer, not a simple event dict) is the special case that follows the
# ``_github`` path instead. Keys MUST match the plugin ``name=`` (and the
# ``connector_accounts.connector`` value) so binding resolution lines up — note
# the email connector's name is ``email-sender``, not ``email``.
OUTBOUND_EVENT_BUILDERS: dict[str, OutboundEventBuilder] = {
    "notion": build_notion_event,
    "slack": build_slack_event,
    "email-sender": build_email_event,
    "telegram": build_telegram_event,
    "discord": build_discord_event,
    "linear": build_linear_event,
    "trello": build_trello_event,
    "sentry": build_sentry_event,
}


__all__ = [
    "OUTBOUND_EVENT_BUILDERS",
    "OutboundEventBuilder",
    "ShapedEvent",
    "_split_summary",
    "_summary_with_refs",
    "build_discord_event",
    "build_email_event",
    "build_linear_event",
    "build_notion_event",
    "build_sentry_event",
    "build_slack_event",
    "build_telegram_event",
    "build_trello_event",
]
