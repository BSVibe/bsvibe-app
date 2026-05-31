"""Linear connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.extensions.plugin.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.extensions.plugin.PluginBuilder`). Every external
call goes through :class:`~.client.LinearClient` (Linear GraphQL API).

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.

A Linear issue is a *new artifact*: once created it cannot be cleanly
hard-deleted via the public API — the best undo is to archive (cancel) it.
Hence the outbound declares ``compensation_tier="t3_new_artifact"`` (Workflow
§9.1), mirroring the notion created-page tier.

There is no ``@p.inbound`` capability: Linear inbound webhooks are a later
chunk.
"""

from __future__ import annotations

import os
from typing import Any

from backend.extensions.implementations.linear.client import (
    DEFAULT_BASE_URL,
    LinearApiError,
    LinearClient,
)
from backend.extensions.plugin import plugin
from backend.extensions.plugin.context import SkillContext

p = plugin(
    name="linear",
    version="0.1.0",
    description="Linear connector — create issues from deliverables with compensation (archive).",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "api_key",
            "description": "Linear personal API key (lin_api_...), sent raw in Authorization.",
            "required": True,
        },
    ],
)

# Linear error codes that mean "the issue is already gone" — treated as an
# idempotent no-op success during compensation (Workflow §9).
_NOT_FOUND_CODES = frozenset({"entityNotFound", "ENTITY_NOT_FOUND"})


def _client(context: SkillContext) -> LinearClient:
    """Build an authed client from the injected credentials.

    ``config['linear_api_url']`` overrides the API base. Raises ``ValueError``
    (→ ``PluginRunError`` at the runner boundary) when no api_key credential is
    present.
    """
    api_key = context.credentials.get("api_key")
    if not api_key:
        raise ValueError("linear: missing required 'api_key' credential")
    base_url = context.config.get("linear_api_url", DEFAULT_BASE_URL)
    return LinearClient(api_key, base_url=base_url)


def _team_id(context: SkillContext, event: dict[str, Any]) -> str:
    """Resolve the Linear team id from the event, falling back to config."""
    team = event.get("team_id") or context.config.get("linear_team_id")
    if not team:
        raise ValueError("linear: missing 'team_id' (event) / 'linear_team_id' (config)")
    return str(team)


def _is_already_gone(exc: LinearApiError) -> bool:
    """Whether a GraphQL error means the issue no longer exists (idempotent undo)."""
    for err in exc.errors:
        if not isinstance(err, dict):
            continue
        code = (
            err.get("extensions", {}).get("code")
            if isinstance(err.get("extensions"), dict)
            else None
        )
        if code in _NOT_FOUND_CODES:
            return True
        if str(err.get("code")) in _NOT_FOUND_CODES:
            return True
    return False


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["issue"],
    compensation_tier="t3_new_artifact",
    compensation_supported=True,
)
async def deliver_issue(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Create a Linear issue from a deliverable on the resolved team."""
    team_id = _team_id(context, event)
    client = _client(context)
    description = event.get("description")
    if description is None:
        description = event.get("body", "")
    issue = await client.create_issue(
        team_id=team_id,
        title=event["title"],
        description=description,
    )
    issue_id = str(issue["id"])
    return {
        "artifact_type": "issue",
        "external_ref": f"linear://issue/{issue_id}",
        "url": issue.get("url"),
        "compensation_handle": {
            "kind": "issue",
            "issue_id": issue_id,
            "identifier": issue.get("identifier"),
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["issue"])
async def revert_issue(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Archive the issue (T3 — the issue becomes a new, archived artifact; the
    public API cannot hard-delete it in place). Idempotent: an already-archived
    / not-found issue (GraphQL ``entityNotFound``) is treated as success."""
    issue_id = str(handle["issue_id"])
    client = _client(context)
    try:
        await client.archive_issue(issue_id)
        already = False
    except LinearApiError as exc:
        if not _is_already_gone(exc):
            raise
        already = True
    return {
        "status": "partially_compensated",
        "tier": "t3_new_artifact",
        "already": already,
        "summary": (f"issue {issue_id} already gone" if already else f"archived issue {issue_id}"),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="create_issue",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["team_id", "title"],
        "properties": {
            "team_id": {"type": "string", "description": "Linear team id"},
            "title": {"type": "string"},
            "description": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def create_issue(
    context: SkillContext,
    team_id: str,
    title: str,
    description: str = "",
) -> dict[str, Any]:
    client = _client(context)
    issue = await client.create_issue(team_id=team_id, title=title, description=description)
    issue_id = str(issue["id"])
    return {
        "issue_id": issue_id,
        "identifier": issue.get("identifier"),
        "url": issue.get("url"),
        "external_ref": f"linear://issue/{issue_id}",
    }


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the linear connector.

    Reads ``LINEAR_API_KEY`` (personal API key) and the optional default
    ``LINEAR_TEAM_ID`` from the environment and persists them under the
    ``linear`` namespace. Env-based ingestion keeps secrets out of shell
    history and process args (python-security) and stays non-interactive for
    CI / headless setup.
    """
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise ValueError("linear setup: set LINEAR_API_KEY (personal API key) in the environment")
    data: dict[str, Any] = {"api_key": api_key}
    team_id = os.environ.get("LINEAR_TEAM_ID")
    if team_id:
        data["linear_team_id"] = team_id
    await cred_store.store("linear", data)
    return data
