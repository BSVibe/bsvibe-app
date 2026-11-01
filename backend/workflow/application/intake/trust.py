"""Who may steer a run from GitHub — the webhook's association, else the person's repo permission.

On an org-owned repo GitHub reports a member whose org membership is PRIVATE as
``CONTRIBUTOR`` (prod #1279, 2026-10-30: the founder's own issue edit was refused). So an
association that is not already trusted is checked against the person's permission on the
repo; ``admin`` / ``maintain`` / ``write`` may steer. A lookup that fails or is not wired
trusts no one beyond the association.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog

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


__all__ = ["TRUSTED_ASSOCIATIONS", "WRITE_PERMISSIONS", "RepoPermission", "is_trusted"]
