"""Connector kind classification — inbound / outbound / both (Lift B).

The founder UI needs to differentiate connectors that *receive* knowledge
(inbound — Obsidian vault scans, Claude/GPT conversation exports, Notion
page reads) from connectors that *deliver* finished work back out
(outbound — Slack post, email send, Notion page create). Some connectors
do both (Notion has BOTH an outbound page-create and an inbound page-read).

The kind here is a small, hardcoded map keyed by connector name. We
intentionally do NOT derive it from the plugin registry at runtime: the
registry only knows about loaded capabilities (an ``@p.action`` named
``import_X`` could mean anything), and the PWA needs the classification
at form-render time before any binding exists. A static map keeps the
classification a v8-style architectural fact that surfaces wherever it's
needed — the API validator, the connector-kind response field, and the
``import`` endpoint's kind gate all read from this one source.

A connector that does NOT appear here is rejected as unknown by the
:class:`backend.api.v1.connectors.ConnectorCreate` validator (alongside the
existing webhook-parser-registered + ``OUTBOUND_EVENT_BUILDERS`` known
sets the validator already consults).

``INBOUND_IMPORT_ACTIONS`` is the inbound counterpart to
:data:`backend.workflow.application.delivery.connector_dispatch.OUTBOUND_EVENT_BUILDERS`:
it names the connector's import-trigger ``@p.action`` so the
:func:`POST /api/v1/connectors/{id}/import` endpoint can resolve the
correct capability without hardcoding action names in the route.
"""

from __future__ import annotations

from typing import Literal

ConnectorKind = Literal["inbound", "outbound", "both"]


# The static connector-kind classification. The set of keys here is the
# *union* of the founder-visible connector names — the API validator
# consults this map alongside the inbound webhook registry + the outbound
# event-builder map to decide what's registerable.
CONNECTOR_KINDS: dict[str, ConnectorKind] = {
    # Outbound delivery connectors (existing).
    "github": "outbound",
    "slack": "both",
    "telegram": "outbound",
    "discord": "outbound",
    "sentry": "outbound",
    "email-sender": "outbound",
    # Inbound knowledge-import connectors (Lift Q3 + Lift B).
    "obsidian": "inbound",
    "claude": "inbound",
    "gpt": "inbound",
    # Notion does BOTH — outbound page create (existing) AND inbound page
    # read (Lift Q3-Notion).
    "notion": "both",
}


# Connector name → import-trigger ``@p.action`` name. The
# :func:`POST /api/v1/connectors/{id}/import` endpoint dispatches the
# named action through :class:`PluginRunner.dispatch_action` with the
# bound :attr:`ConnectorAccountRow.delivery_config` injected into the
# ``SkillContext.config``. Outbound-only connectors have no entry here
# and the endpoint 422s on a binding whose connector is not inbound/both.
INBOUND_IMPORT_ACTIONS: dict[str, str] = {
    "obsidian": "import_vault",
    "claude": "import_conversations",
    "gpt": "import_conversations",
    "notion": "import_pages",
    # ``slack`` is "both" in the kind map (it has an inbound webhook parser
    # for app-mention deliveries) but it has NO bulk-import action — the
    # inbound path is push-only (the webhook ingress drives it). Absence
    # here is intentional: the import endpoint will 422 with a clear
    # "no bulk import for this connector" error.
}


def connector_kind(name: str) -> ConnectorKind | None:
    """Return the static kind for ``name`` or ``None`` if unknown."""
    return CONNECTOR_KINDS.get(name)


def is_known_connector(name: str) -> bool:
    """True when ``name`` appears in the kind map."""
    return name in CONNECTOR_KINDS


def is_inbound(name: str) -> bool:
    """True when the connector can RECEIVE knowledge (kind inbound/both)."""
    return CONNECTOR_KINDS.get(name) in ("inbound", "both")


def is_outbound(name: str) -> bool:
    """True when the connector can DELIVER work (kind outbound/both)."""
    return CONNECTOR_KINDS.get(name) in ("outbound", "both")


def import_action_for(name: str) -> str | None:
    """Return the import-trigger ``@p.action`` name for ``name``.

    ``None`` means this connector has no bulk-import action wired —
    either it's outbound-only, or its inbound path is push-only
    (webhook-driven, like ``slack``).
    """
    return INBOUND_IMPORT_ACTIONS.get(name)


__all__ = [
    "CONNECTOR_KINDS",
    "INBOUND_IMPORT_ACTIONS",
    "ConnectorKind",
    "connector_kind",
    "import_action_for",
    "is_inbound",
    "is_known_connector",
    "is_outbound",
]
