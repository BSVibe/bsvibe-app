"""Lift §17.9 — deliverables.py decomp invariants.

Asserts:
  1. ``backend.api.v1.deliverables`` resolves to a *package* (a directory,
     not a 749-LOC single-file module). One sub-file per endpoint grouping
     (list_get / proof / retract) plus shared schemas + helpers.
  2. The public surface used by ``backend.api.v1.__init__`` and existing
     tests is preserved — ``router`` and ``get_retract_handler`` remain
     importable from ``backend.api.v1.deliverables``.
  3. Every endpoint URL the legacy module exposed still routes — the
     FastAPI route-set under the ``/deliverables`` prefix is unchanged
     vs the known list (LIST/GET/REPORT/ARTIFACT/RETRACT).
  4. Each sub-file ≤ 250 LOC (D35 thin-adapter ceiling).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_deliverables_is_a_package() -> None:
    """deliverables resolves to a *directory* package, not a single .py file."""
    mod = importlib.import_module("backend.api.v1.deliverables")
    assert hasattr(mod, "__path__"), (
        "expected backend.api.v1.deliverables to be a package (directory) "
        "after Lift §17.9 decomp; still a single-file module"
    )


def test_router_and_retract_handler_reexported() -> None:
    """Public surface preserved — existing call sites still work."""
    from backend.api.v1.deliverables import get_retract_handler, router

    assert router is not None
    assert callable(get_retract_handler)


def test_all_endpoint_urls_still_routable() -> None:
    """The 6 deliverables endpoints remain reachable via the aggregator router."""
    from backend.api.v1.deliverables import router

    methods_and_paths: set[tuple[str, str]] = set()
    for route in router.routes:
        for method in getattr(route, "methods", set()) or set():
            if method == "HEAD":
                continue
            methods_and_paths.add((method, getattr(route, "path", "")))

    expected = {
        ("GET", ""),
        ("GET", "/{deliverable_id}"),
        ("GET", "/{deliverable_id}/report"),
        ("GET", "/{deliverable_id}/diff"),
        ("GET", "/{deliverable_id}/artifacts/{ref:path}"),
        ("POST", "/{deliverable_id}/retract"),
    }
    assert expected <= methods_and_paths, (
        f"missing endpoints after decomp: {expected - methods_and_paths}"
    )


@pytest.mark.parametrize(
    "submodule",
    [
        "backend.api.v1.deliverables.list_get",
        "backend.api.v1.deliverables.proof",
        "backend.api.v1.deliverables.diff",
        "backend.api.v1.deliverables.retract",
        "backend.api.v1.deliverables._narrative",
        "backend.api.v1.deliverables._references",
        "backend.api.v1.deliverables._schemas",
    ],
)
def test_submodule_under_250_loc(submodule: str) -> None:
    """D35 thin-adapter ceiling — each sub-file ≤ 250 LOC."""
    mod = importlib.import_module(submodule)
    path = Path(mod.__file__ or "")
    assert path.exists(), f"{submodule}: missing file {path}"
    loc = sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
    assert loc <= 250, f"{submodule}: {loc} LOC exceeds 250-LOC ceiling"
