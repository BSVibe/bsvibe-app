"""Lift M1 — REST handler split invariants (v8 §20 Pattern A).

The five mid-sized REST handler god-files (``runs.py`` / ``inside.py`` /
``products.py`` / ``safemode.py`` / ``decisions.py``) are decomposed into
``<endpoint>/`` packages mirroring the §17.9 ``deliverables/`` template:

* ``<endpoint>/__init__.py`` — aggregator router (single ``router`` object
  the v1 aggregator mounts, built by extending the sub-routers' ``routes``).
* One sub-module per endpoint grouping (read / write / etc.).
* ``_schemas.py`` for shared Pydantic types when used across sub-files.
* ``_helpers.py`` (and dependency builders) for shared adapter helpers.

Each sub-file stays under the D35 thin-adapter ceiling (≤ 300 LOC).

Asserts (RED-first):
  1. Each target resolves to a *package* (directory), not a single .py file.
  2. The public surface used by other modules + tests is preserved — every
     attribute already imported from these modules elsewhere in the tree
     remains importable.
  3. The FastAPI route set under each prefix is byte-for-byte identical to
     the pre-decomp set (URLs unchanged, methods unchanged).
  4. Each sub-file ≤ 300 LOC (D35 thin-adapter ceiling).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# Pre-decomp route signatures (captured from reading the legacy files before
# the lift). Each tuple is ``(METHOD, PATH)`` exactly as FastAPI records it on
# the per-module APIRouter — the v1 aggregator mounts each under its own
# ``prefix=`` so the URL set seen by clients stays unchanged when the prefix
# concatenates onto these paths.
EXPECTED_ROUTES: dict[str, set[tuple[str, str]]] = {
    "backend.api.v1.runs": {
        ("GET", ""),
        ("GET", "/{run_id}"),
        ("GET", "/{run_id}/detail"),
    },
    "backend.api.v1.inside": {
        ("GET", "/concepts"),
        ("GET", "/concepts/{concept_id}"),
        ("GET", "/observations"),
        ("GET", "/graph"),
        # Lift M3a — ontology retraction / correction surface.
        ("POST", "/nodes/{node_ref:path}/retract"),
        ("POST", "/nodes/{node_ref:path}/correct"),
        ("POST", "/corrections/{correction_id}/undo"),
        # Lift M4a — proof surface (Fleet + Inside trust panel).
        ("GET", "/trust/fleet"),
        ("GET", "/trust/{product_id}"),
    },
    "backend.api.v1.products": {
        ("GET", ""),
        ("POST", ""),
        ("GET", "/{product_id}"),
        ("PATCH", "/{product_id}"),
        ("DELETE", "/{product_id}"),
        ("GET", "/{product_id}/resources"),
        ("POST", "/{product_id}/resources"),
        ("DELETE", "/{product_id}/resources/{resource_id}"),
        ("GET", "/{product_id}/bindings"),
        ("POST", "/{product_id}/bindings"),
        ("PATCH", "/{product_id}/bindings/{binding_id}"),
        ("DELETE", "/{product_id}/bindings/{binding_id}"),
        ("GET", "/{product_id}/files"),
        ("GET", "/{product_id}/files/content"),
    },
    "backend.api.v1.safemode": {
        ("GET", "/queue"),
        ("GET", "/queue/by-run"),
        ("GET", "/resolved"),
        ("POST", "/runs/{run_id}/approve"),
        ("POST", "/{item_id}/approve"),
        ("POST", "/{item_id}/deny"),
    },
    "backend.api.v1.decisions": {
        ("GET", ""),
        ("GET", "/log"),
        ("POST", "/{proposal_id:path}/accept"),
        ("POST", "/{proposal_id:path}/reject"),
    },
}


# Public-surface attributes other modules / tests already import. The lift
# MUST keep each of these importable from the package's ``__init__.py``.
PUBLIC_SURFACE: dict[str, list[str]] = {
    "backend.api.v1.runs": ["router"],
    "backend.api.v1.inside": ["router", "build_inside_storage", "build_inside_index"],
    "backend.api.v1.products": ["router"],
    "backend.api.v1.safemode": ["router", "get_delivery_dispatcher"],
    "backend.api.v1.decisions": [
        "router",
        "_vault_root",
        "build_canonicalization_index",
        "build_canonicalization_service",
    ],
}


@pytest.mark.parametrize("modname", list(EXPECTED_ROUTES.keys()))
def test_module_is_a_package(modname: str) -> None:
    """Each target resolves to a *directory* package after Lift M1."""
    mod = importlib.import_module(modname)
    assert hasattr(mod, "__path__"), (
        f"expected {modname} to be a package (directory) after Lift M1 decomp; "
        "still a single-file module"
    )


@pytest.mark.parametrize("modname", list(PUBLIC_SURFACE.keys()))
def test_public_surface_preserved(modname: str) -> None:
    """Re-exports used by callers + tests remain importable from the package."""
    mod = importlib.import_module(modname)
    for attr in PUBLIC_SURFACE[modname]:
        assert hasattr(mod, attr), f"{modname} missing public attribute {attr!r} after decomp"


@pytest.mark.parametrize("modname", list(EXPECTED_ROUTES.keys()))
def test_route_set_unchanged(modname: str) -> None:
    """The FastAPI route set under each module's router matches the pre-decomp set."""
    mod = importlib.import_module(modname)
    router = mod.router  # type: ignore[attr-defined]

    actual: set[tuple[str, str]] = set()
    for route in router.routes:
        for method in getattr(route, "methods", set()) or set():
            if method == "HEAD":
                continue
            actual.add((method, getattr(route, "path", "")))

    expected = EXPECTED_ROUTES[modname]
    assert expected <= actual, (
        f"{modname}: missing endpoints after decomp: {expected - actual}\n"
        f"  expected: {sorted(expected)}\n"
        f"  actual:   {sorted(actual)}"
    )
    # And no extras — the lift is a pure split, never an addition.
    assert actual <= expected, (
        f"{modname}: unexpected new endpoints introduced by decomp: {actual - expected}"
    )


# Sub-files we expect to land — used to assert the LOC ceiling.
EXPECTED_SUBMODULES: dict[str, list[str]] = {
    "backend.api.v1.runs": [
        "backend.api.v1.runs.list_get",
        "backend.api.v1.runs.detail",
        "backend.api.v1.runs._schemas",
        "backend.api.v1.runs._helpers",
    ],
    "backend.api.v1.inside": [
        "backend.api.v1.inside.concepts",
        "backend.api.v1.inside.observations",
        "backend.api.v1.inside.graph",
        "backend.api.v1.inside.retraction",
        "backend.api.v1.inside.trust",
        "backend.api.v1.inside._schemas",
        "backend.api.v1.inside._dependencies",
        "backend.api.v1.inside._helpers",
    ],
    "backend.api.v1.products": [
        "backend.api.v1.products.products_crud",
        "backend.api.v1.products.resources",
        "backend.api.v1.products.bindings",
        "backend.api.v1.products.files",
        "backend.api.v1.products._schemas",
        "backend.api.v1.products._helpers",
    ],
    "backend.api.v1.safemode": [
        "backend.api.v1.safemode.list_get",
        "backend.api.v1.safemode.mutations",
        "backend.api.v1.safemode._schemas",
        "backend.api.v1.safemode._helpers",
    ],
    "backend.api.v1.decisions": [
        "backend.api.v1.decisions.list_get",
        "backend.api.v1.decisions.resolve",
        "backend.api.v1.decisions._schemas",
        "backend.api.v1.decisions._helpers",
    ],
}


@pytest.mark.parametrize(
    "submodule",
    [sub for subs in EXPECTED_SUBMODULES.values() for sub in subs],
)
def test_submodule_under_loc_ceiling(submodule: str) -> None:
    """D35 thin-adapter ceiling — each sub-file ≤ 300 LOC."""
    mod = importlib.import_module(submodule)
    path = Path(mod.__file__ or "")
    assert path.exists(), f"{submodule}: missing file {path}"
    loc = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
    assert loc <= 300, f"{submodule}: {loc} LOC exceeds 300-LOC ceiling"
