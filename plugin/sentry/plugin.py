"""Sentry connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.SentryClient`; the inbound parser lives in
:mod:`~.webhook`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.

Compensation tier (Workflow §9.1): the outbound *resolves* a Sentry issue.
Resolving is reversible — re-opening (``status: "unresolved"``) is a first-class
Sentry operation — but Sentry keeps an activity trail of the resolve/unresolve
on the issue. So the artifact is reversible-with-trail → ``t2_trail``.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.plugin.context import SkillContext
from backend.workflow.domain.incoming import TriggerEvent
from bsvibe_sdk import plugin
from plugin.sentry.client import (
    DEFAULT_BASE_URL,
    SentryApiError,
    SentryClient,
)
from plugin.sentry.webhook import parse_webhook

p = plugin(
    name="sentry",
    version="0.1.0",
    description="Sentry connector — issue webhook intake + resolve-issue delivery with compensation.",
    author="BSVibe",
    data_jurisdiction="us",  # sentry.io SaaS is US-hosted (self-hosted / EU regions exist).
    credentials=[
        {
            "name": "auth_token",
            "description": "Sentry auth token (Bearer) with project:write / event:write scope.",
            "required": True,
        },
        {
            "name": "client_secret",
            "description": "Sentry integration client-secret used to verify inbound HMAC signatures.",
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> SentryClient:
    """Build an authed client from the injected credentials.

    ``config['sentry_api_url']`` overrides the API base (self-hosted / EU
    region). Raises ``ValueError`` (→ ``PluginRunError`` at the runner
    boundary) when no auth token credential is present.
    """
    token = context.credentials.get("auth_token")
    if not token:
        raise ValueError("sentry: missing required 'auth_token' credential")
    base_url = context.config.get("sentry_api_url", DEFAULT_BASE_URL)
    return SentryClient(token, base_url=base_url)


# ── inbound ──────────────────────────────────────────────────────────────────


@p.inbound(trigger={"type": "webhook"})
async def on_webhook(context: SkillContext, payload: dict[str, Any]) -> TriggerEvent | None:
    """Parse a Sentry webhook delivery into a TriggerEvent (or None to skip).

    Expected ``payload`` shape (populated by the intake webhook route — out of
    this track's scope)::

        {"workspace_id": UUID, "headers": {...}, "raw_body": bytes}
    """
    raw_body = payload["raw_body"]
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    secret = context.credentials.get("client_secret")
    return parse_webhook(
        workspace_id=payload["workspace_id"],
        headers=payload.get("headers", {}),
        raw_body=raw_body,
        secret=secret,
    )


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["sentry_issue_update"],
    compensation_tier="t2_trail",
    compensation_supported=True,
)
async def deliver_resolve(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Resolve a Sentry issue. Reversible (re-open via ``status:unresolved``),
    but Sentry records the resolve in the issue activity trail → ``t2_trail``."""
    issue_id = str(event["issue_id"])
    client = _client(context)
    data = await client.update_issue_status(issue_id, "resolved")
    return {
        "artifact_type": "sentry_issue_update",
        "external_ref": f"sentry://issue/{issue_id}",
        "url": data.get("permalink"),
        "compensation_handle": {
            "kind": "issue_status",
            "issue_id": issue_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["sentry_issue_update"])
async def revert_resolve(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Re-open the resolved issue (``status:unresolved``) — T2, the
    resolve/unresolve flip leaves a Sentry activity trail. Idempotent: a
    404 (issue already gone) is treated as a silent no-op success."""
    issue_id = str(handle["issue_id"])
    client = _client(context)
    try:
        await client.update_issue_status(issue_id, "unresolved")
        already = False
    except SentryApiError as exc:
        if exc.status_code != 404:
            raise
        already = True
    return {
        "status": "partially_compensated",
        "tier": "t2_trail",
        "already": already,
        "summary": (
            f"issue {issue_id} already gone"
            if already
            else f"re-opened issue {issue_id} (trail remains)"
        ),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="resolve_issue",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["issue_id"],
        "properties": {
            "issue_id": {"type": "string", "description": "Sentry issue (group) id"},
        },
        "additionalProperties": False,
    },
)
async def resolve_issue(context: SkillContext, issue_id: str) -> dict[str, Any]:
    client = _client(context)
    data = await client.update_issue_status(str(issue_id), "resolved")
    return {
        "issue_id": str(issue_id),
        "status": data.get("status", "resolved"),
        "url": data.get("permalink"),
        "external_ref": f"sentry://issue/{issue_id}",
    }


# M2 — read-only action: agent queries the project's current unresolved Sentry
# issues mid-run for context (triaging a bug report, checking whether an error
# the run is about to fix is already known). Read-only by construction (GET).
@p.action(
    name="list_issues",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["organization_slug", "project_slug"],
        "properties": {
            "organization_slug": {
                "type": "string",
                "description": "Sentry organization slug",
            },
            "project_slug": {
                "type": "string",
                "description": "Sentry project slug within the organization",
            },
            "query": {
                "type": "string",
                "description": (
                    "Sentry issue search query (e.g. 'is:unresolved', "
                    "'is:unresolved error.type:KeyError'). Defaults to "
                    "'is:unresolved'."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Max issues to return (default 20, cap 50).",
            },
        },
        "additionalProperties": False,
    },
)
async def list_issues(
    context: SkillContext,
    organization_slug: str,
    project_slug: str,
    query: str = "is:unresolved",
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only — list issues for a Sentry project, shaped for the LLM.

    Each entry is trimmed to ``id``, ``title``, ``status``, ``permalink``,
    ``count``, ``culprit`` so the payload stays inside the LLM response budget."""
    client = _client(context)
    capped = max(1, min(int(limit), 50))
    raw = await client.list_project_issues(
        organization_slug, project_slug, query=query, per_page=capped
    )
    issues = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        issues.append(
            {
                "id": str(entry.get("id", "")),
                "title": str(entry.get("title", "")),
                "status": str(entry.get("status", "")),
                "permalink": entry.get("permalink"),
                "count": entry.get("count"),
                "culprit": entry.get("culprit"),
            }
        )
        if len(issues) >= capped:
            break
    return {
        "organization_slug": organization_slug,
        "project_slug": project_slug,
        "query": query,
        "count": len(issues),
        "issues": issues,
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the sentry connector.

    Reads ``SENTRY_AUTH_TOKEN`` (Bearer auth token) and the optional
    ``SENTRY_CLIENT_SECRET`` (inbound HMAC verification) from the environment
    and persists them under the ``sentry`` namespace. Env-based ingestion keeps
    secrets out of shell history and process args (python-security) and stays
    non-interactive for CI / headless setup.
    """
    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        raise ValueError(
            "sentry setup: set SENTRY_AUTH_TOKEN (Bearer auth token) in the environment"
        )
    data: dict[str, Any] = {"auth_token": token}
    secret = os.environ.get("SENTRY_CLIENT_SECRET")
    if secret:
        data["client_secret"] = secret
    await cred_store.store("sentry", data)
    return data
