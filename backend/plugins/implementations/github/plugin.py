"""GitHub connector plugin — capability registrations (Workflow §6 #4, §9).

The :class:`backend.plugins.PluginLoader` imports this file and picks up the
module-level ``p`` (a :class:`backend.plugins.PluginBuilder`). Every external
call goes through :class:`~.client.GithubClient`; the inbound parser lives in
:mod:`~.webhook`.

Outbound functions return a plain ``dict`` — the
:class:`backend.delivery.dispatcher.DeliveryDispatcher` wraps it into an
``ActionResult.output`` and builds the persisted ``DeliveryResult``. The dict
carries ``external_ref`` / ``url`` / ``compensation_handle`` (Workflow §3.1)
so the matching ``@p.compensate`` handler can later revert by handle.
"""

from __future__ import annotations

import os
from typing import Any

from backend.intake.schema import TriggerEvent
from backend.plugins import plugin
from backend.plugins.context import SkillContext
from backend.plugins.implementations.github.client import DEFAULT_BASE_URL, GithubClient
from backend.plugins.implementations.github.webhook import parse_webhook

p = plugin(
    name="github",
    version="0.1.0",
    description="GitHub connector — webhook intake + PR/comment delivery with compensation.",
    author="BSVibe",
    data_jurisdiction="us",
    credentials=[
        {
            "name": "token",
            "description": "GitHub PAT or OAuth access token (repo + pull_request scope).",
            "required": True,
        },
        {
            "name": "webhook_secret",
            "description": "HMAC secret used to verify inbound webhook signatures.",
            "required": False,
        },
    ],
)


def _client(context: SkillContext) -> GithubClient:
    """Build an authed client from the injected credentials.

    ``config['github_api_url']`` overrides the API base (GitHub Enterprise).
    Raises ``ValueError`` (→ ``PluginRunError`` at the runner boundary) when no
    token credential is present.
    """
    token = context.credentials.get("token")
    if not token:
        raise ValueError("github: missing required 'token' credential")
    base_url = context.config.get("github_api_url", DEFAULT_BASE_URL)
    return GithubClient(token, base_url=base_url)


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"github: invalid repo {repo!r}, expected 'owner/repo'")
    return owner, name


# ── inbound ──────────────────────────────────────────────────────────────────


@p.inbound(trigger={"type": "webhook"})
async def on_webhook(context: SkillContext, payload: dict[str, Any]) -> TriggerEvent | None:
    """Parse a GitHub webhook delivery into a TriggerEvent (or None to skip).

    Expected ``payload`` shape (populated by the intake webhook route — out of
    this track's scope)::

        {"workspace_id": UUID, "headers": {...}, "raw_body": bytes}
    """
    raw_body = payload["raw_body"]
    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    secret = context.credentials.get("webhook_secret")
    return parse_webhook(
        workspace_id=payload["workspace_id"],
        headers=payload.get("headers", {}),
        raw_body=raw_body,
        secret=secret,
    )


# ── outbound ─────────────────────────────────────────────────────────────────


@p.outbound(
    artifact_types=["code", "pr"], compensation_tier="t2_trail", compensation_supported=True
)
async def deliver_pr(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Open a new PR, or update the existing one when ``pr_number`` is given."""
    owner, repo = _split_repo(event["repo"])
    client = _client(context)
    number = event.get("pr_number")
    if number is not None:
        data = await client.update_pr(
            owner, repo, int(number), title=event.get("title"), body=event.get("body")
        )
    else:
        data = await client.open_pr(
            owner,
            repo,
            head=event["head"],
            base=event.get("base", "main"),
            title=event["title"],
            body=event.get("body", ""),
        )
        number = data["number"]
    return {
        "artifact_type": "pr",
        "external_ref": f"github://{owner}/{repo}/pull/{number}",
        "url": data.get("html_url"),
        "compensation_handle": {
            "kind": "pr",
            "owner": owner,
            "repo": repo,
            "number": int(number),
        },
    }


@p.outbound(
    artifact_types=["issue_comment"], compensation_tier="t1_clean", compensation_supported=True
)
async def deliver_comment(context: SkillContext, event: dict[str, Any]) -> dict[str, Any]:
    """Post a comment on an issue/PR thread."""
    owner, repo = _split_repo(event["repo"])
    issue_number = int(event["issue_number"])
    client = _client(context)
    data = await client.post_comment(owner, repo, issue_number, event["body"])
    comment_id = int(data["id"])
    return {
        "artifact_type": "issue_comment",
        "external_ref": f"github://{owner}/{repo}/issues/{issue_number}#issuecomment-{comment_id}",
        "url": data.get("html_url"),
        "compensation_handle": {
            "kind": "comment",
            "owner": owner,
            "repo": repo,
            "comment_id": comment_id,
        },
    }


# ── compensation (idempotent — Workflow §9) ────────────────────────────────────


@p.compensate(artifact_types=["code", "pr"])
async def revert_pr(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Close the PR (T2 — commits remain on the branch). Idempotent: a PR that
    is already closed yields a silent no-op success."""
    owner, repo, number = handle["owner"], handle["repo"], int(handle["number"])
    client = _client(context)
    pr = await client.get_pr(owner, repo, number)
    if pr.get("state") == "closed":
        return {
            "status": "partially_compensated",
            "tier": "t2_trail",
            "already": True,
            "summary": f"PR #{number} already closed",
        }
    await client.close_pr(owner, repo, number)
    return {
        "status": "partially_compensated",
        "tier": "t2_trail",
        "already": False,
        "summary": f"closed PR #{number} (commits remain on branch)",
    }


@p.compensate(artifact_types=["issue_comment"])
async def revert_comment(context: SkillContext, handle: dict[str, Any]) -> dict[str, Any]:
    """Delete the comment (T1 — clean). Idempotent: a 404 (already deleted) is
    treated as success."""
    owner, repo, comment_id = handle["owner"], handle["repo"], int(handle["comment_id"])
    client = _client(context)
    status = await client.delete_comment(owner, repo, comment_id)
    already = status == 404
    return {
        "status": "compensated",
        "tier": "t1_clean",
        "already": already,
        "summary": (
            f"comment {comment_id} already gone" if already else f"deleted comment {comment_id}"
        ),
    }


# ── actions (agent-loop tools) ──────────────────────────────────────────────────


@p.action(
    name="open_pr",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["repo", "head", "title"],
        "properties": {
            "repo": {"type": "string", "description": "owner/repo"},
            "head": {"type": "string", "description": "source branch"},
            "base": {"type": "string", "description": "target branch (default main)"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def open_pr(
    context: SkillContext,
    repo: str,
    head: str,
    title: str,
    base: str = "main",
    body: str = "",
) -> dict[str, Any]:
    owner, name = _split_repo(repo)
    client = _client(context)
    data = await client.open_pr(owner, name, head=head, base=base, title=title, body=body)
    number = int(data["number"])
    return {
        "pr_number": number,
        "url": data.get("html_url"),
        "external_ref": f"github://{owner}/{name}/pull/{number}",
    }


@p.action(
    name="comment",
    mcp_exposed=True,
    input_schema={
        "type": "object",
        "required": ["repo", "issue_number", "body"],
        "properties": {
            "repo": {"type": "string", "description": "owner/repo"},
            "issue_number": {"type": "integer"},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    },
)
async def comment(context: SkillContext, repo: str, issue_number: int, body: str) -> dict[str, Any]:
    owner, name = _split_repo(repo)
    client = _client(context)
    data = await client.post_comment(owner, name, int(issue_number), body)
    return {"comment_id": int(data["id"]), "url": data.get("html_url")}


# ── setup ────────────────────────────────────────────────────────────────────


@p.setup
async def setup(cred_store: Any) -> dict[str, Any]:
    """Credential flow for the github connector.

    Reads ``GITHUB_TOKEN`` (PAT or OAuth access token) and the optional
    ``GITHUB_WEBHOOK_SECRET`` from the environment and persists them under the
    ``github`` namespace. Env-based ingestion keeps secrets out of shell
    history and process args (python-security) and stays non-interactive for
    CI / headless setup.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "github setup: set GITHUB_TOKEN (PAT or OAuth access token) in the environment"
        )
    data: dict[str, Any] = {"token": token}
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if secret:
        data["webhook_secret"] = secret
    await cred_store.store("github", data)
    return data
