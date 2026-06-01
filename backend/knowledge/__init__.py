"""Knowledge module — vault graph, canonicalization, ingest, retrieval, MCP.

Contract (Lift N-Coverage pattern #8):

* **Owns** the per-workspace vault graph and its lifecycle — ingest of
  candidate artifacts, canonicalization (proposals + decisions), retrieval
  over canon-stable notes, and the settle scheduler that drains proposals
  through their decision states.
* **Facade**: ``backend.knowledge.facade.Knowledge`` Protocol exposing
  ``ingest`` / ``retrieve_canon`` / ``settle`` (v8 §5.2).
* **Not exposed**: vault filesystem layout, NetworkX graph internals,
  embedding backends, and canonicalization scoring are private —
  callers depend on the ``Knowledge`` facade + ``KnowledgeFactory``.

Workspace scoping is enforced at the :class:`KnowledgeFactory` layer, which
constructs per-workspace ``Vault`` instances rooted at
``<vault_root>/<region>/<workspace_id>/``.
"""

from __future__ import annotations

from backend.knowledge.factory import KnowledgeFactory, WorkspaceContext

__all__ = ["KnowledgeFactory", "WorkspaceContext"]
