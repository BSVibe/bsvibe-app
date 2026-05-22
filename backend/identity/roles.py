"""RBAC role hierarchy on ``Membership.role`` (Workflow §3 / §10.6).

The four roles are totally ordered: ``owner > admin > editor > viewer``.
``role_satisfies`` is the single comparison primitive used by the
``require_role`` FastAPI dependency — authorization is one orthogonal axis
(what a member can do) on top of authentication (Supabase JWT) and isolation
(``workspace_id`` scoping).
"""

from __future__ import annotations

from typing import Final

ROLE_HIERARCHY: Final[dict[str, int]] = {
    "viewer": 0,
    "editor": 1,
    "admin": 2,
    "owner": 3,
}


def role_satisfies(role: str, minimum: str) -> bool:
    """Return True iff ``role`` ranks at or above ``minimum``.

    ``minimum`` must be a known role (a misconfigured gate is a programming
    error, raised eagerly). An unrecognised ``role`` fails closed — it ranks
    below every real role, so a corrupt/legacy membership never escalates.
    """
    if minimum not in ROLE_HIERARCHY:
        raise ValueError(f"unknown role threshold: {minimum!r}")
    return ROLE_HIERARCHY.get(role, -1) >= ROLE_HIERARCHY[minimum]
