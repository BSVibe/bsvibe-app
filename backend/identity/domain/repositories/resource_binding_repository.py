"""ResourceBindingRepository Protocol — read/write seam for ``resource_bindings``.

Lift I-Repo-Final Phase A. The 3-knob *Resource binding* (Workflow §3) row
carries ``selection`` / ``trigger`` / ``output_mode`` for one Product ×
ConnectorAccount pairing. Before this lift, the only seam was the concrete
``backend.workspaces.resource_bindings.ResourceBindingRepository`` class
that exposed both the Protocol shape AND the SQLAlchemy access in one
module. Absorbing :mod:`backend.workspaces` into :mod:`backend.identity`
splits it into the Protocol here + the concrete
:class:`SqlAlchemyResourceBindingRepository` under
:mod:`backend.identity.infrastructure.repositories` (matches the Lift
I-Repo-Identity convention for Workspace / User / Membership).

Method surface preserves every caller's contract verbatim:

* :meth:`create` — workspace-scoped insert (validates ``output_mode``).
* :meth:`get` — workspace-scoped lookup by binding id.
* :meth:`list_for_product` — ordered listing of one Product's bindings.
* :meth:`update` — patch a subset of the 3 knobs (``None`` = leave as-is).
* :meth:`delete` — hard-delete; returns ``False`` if not present.
* :meth:`find_binding` — the Receive-stage lookup (no ``workspace_id``
  parameter — the inbound path already resolved the account to a workspace
  upstream and the binding's ``workspace_id`` matches by construction).

Concrete impl:
:mod:`backend.identity.infrastructure.repositories.resource_binding_repository_sql`.
"""

from __future__ import annotations

# Allowed values for the ``output_mode`` knob (Workflow §3 / §1). Kept here so
# the Protocol module is the single import source for both the seam type AND
# the wire-validated value set callers + tests rely on (mirrors the old
# ``backend.workspaces.resource_bindings`` import surface).
OUTPUT_MODES: frozenset[str] = frozenset({"safe", "direct"})


__all__ = ["OUTPUT_MODES"]
