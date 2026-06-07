"""``/api/v1/products`` aggregator router — per-workspace Product CRUD (Lift M1).

Decomposes the 562-LOC ``products.py`` god-file into thin endpoint-grouping
sub-modules per v8 §20 + D35:

* :mod:`.products_crud` — list / create / get / patch / delete the Product itself.
* :mod:`.bootstrap_actions` — Lift E13 cancel/retry surface for the
  per-product bootstrap (POST ``/{slug_or_id}/bootstrap/{cancel,retry}``).
* :mod:`.resources` — named pointers a product works with (repo / doc /
  deploy / note): list / add / delete.
* :mod:`.bindings` — per-Product × ConnectorAccount 3-knob binding
  (Workflow §3 — ``selection`` / ``trigger`` / ``output_mode``): list /
  create / patch / delete.
* :mod:`.files` — lazy per-directory browser over the product's git ``main``
  tree, plus a single-file content endpoint.

Shared Pydantic schemas live in :mod:`._schemas`; the workspace-scope
resolvers (``_resolve_product_in_workspace`` /
``_resolve_connector_account_in_workspace``) and the file-content caps live
in :mod:`._helpers`.

Workspace isolation is structural — every endpoint scopes to the caller's
workspace via :func:`get_workspace_id` and a cross-workspace row is uniformly
404 (never a leak, never a 500).
"""

from __future__ import annotations

from fastapi import APIRouter

from . import bindings, bootstrap_actions, files, products_crud, resources

# Single aggregator router — see deliverables/__init__.py for the
# ``routes.extend(...)`` rationale.
router = APIRouter()
for _sub in (
    products_crud.router,
    bootstrap_actions.router,
    resources.router,
    bindings.router,
    files.router,
):
    router.routes.extend(_sub.routes)

__all__ = ["router"]
