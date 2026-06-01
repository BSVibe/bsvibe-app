"""Identity domain — the User ↔ Membership ↔ Workspace join (Workflow §3).

Contract (Lift N-Coverage pattern #8):

* **Owns** the User ↔ Membership ↔ Workspace join — maps a Supabase
  subject to a first-class ``UserRow`` + ``MembershipRow`` and bootstraps
  workspaces on first authenticated request (v8 §10.1).
* **Facade**: no Protocol facade yet — callers use the concrete
  :mod:`backend.identity.service` for workspace bootstrap and
  :mod:`backend.identity.repository` for row access.
* **Not exposed**: SQLModel rows, repository implementations, and
  membership-resolution helpers are private — Identity does not
  re-export them at this namespace.

The authenticated principal resolved by ``backend.shared.authz`` carries a
Supabase subject (``User.id``). This package maps that subject to a
first-class ``UserRow`` and, through ``MembershipRow``, to the workspace the
request operates within. Workspace bootstrap (§10.1) lives in
:mod:`backend.identity.service`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
