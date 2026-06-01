"""Identity domain — the User ↔ Membership ↔ Workspace join (Workflow §3).

The authenticated principal resolved by ``backend.shared.authz`` carries a
Supabase subject (``User.id``). This package maps that subject to a
first-class ``UserRow`` and, through ``MembershipRow``, to the workspace the
request operates within. Workspace bootstrap (§10.1) lives in
:mod:`backend.identity.service`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
