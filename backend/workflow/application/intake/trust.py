"""Who may steer a run from GitHub — the webhook's association, else the person's repo permission.

On an org-owned repo GitHub reports a member whose org membership is PRIVATE as
``CONTRIBUTOR`` (prod #1279, 2026-10-30: the founder's own issue edit was refused). So an
association that is not already trusted is checked against the person's permission on the
repo; ``admin`` / ``maintain`` / ``write`` may steer. A lookup that fails or is not wired
trusts no one beyond the association.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog

from backend.workflow.domain.requirements import COMMENT_TRUSTED_KEY

logger = structlog.get_logger(__name__)

#: ``author_association`` values trusted without asking GitHub.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Repo permissions that may steer a run.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

#: ``(workspace_id, "owner/name", login) -> permission`` (``None`` when GitHub has none).
RepoPermission = Callable[[uuid.UUID, str, str], Awaitable[str | None]]


async def is_trusted(
    association: str,
    *,
    login: str,
    repo: str,
    workspace_id: uuid.UUID,
    permission_of: RepoPermission | None,
) -> bool:
    """Whether ``login`` may put an instruction on a run of ``repo``."""
    if association.upper() in TRUSTED_ASSOCIATIONS:
        return True
    if permission_of is None or not login:
        return False
    try:
        permission = await permission_of(workspace_id, repo, login)
    except Exception:  # noqa: BLE001 — an unknown permission trusts no one
        logger.warning(
            "intake_repo_permission_lookup_failed", repo=repo, login=login, exc_info=True
        )
        return False
    return (permission or "").lower() in WRITE_PERMISSIONS


async def stamp_comment_trust(
    payload: Mapping[str, Any],
    *,
    workspace_id: uuid.UUID,
    permission_of: RepoPermission | None,
) -> dict[str, Any]:
    """``payload`` with :data:`COMMENT_TRUSTED_KEY` set when it is an issue comment whose
    author the association alone does not trust but who has write access to the repo — so
    the ledger counts a private org member's checklist comment. Anything else: unchanged."""
    out = dict(payload)
    body = payload.get("body")
    comment = body.get("comment") if isinstance(body, Mapping) else None
    if payload.get("github_event") != "issue_comment" or not isinstance(comment, Mapping):
        return out
    association = str(comment.get("author_association") or "").upper()
    if association in TRUSTED_ASSOCIATIONS:
        return out
    user = comment.get("user")
    login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
    repo = str(payload.get("repo") or "")
    if repo and await is_trusted(
        association, login=login, repo=repo, workspace_id=workspace_id, permission_of=permission_of
    ):
        out[COMMENT_TRUSTED_KEY] = True
    return out


__all__ = [
    "TRUSTED_ASSOCIATIONS",
    "WRITE_PERMISSIONS",
    "RepoPermission",
    "is_trusted",
    "stamp_comment_trust",
]
