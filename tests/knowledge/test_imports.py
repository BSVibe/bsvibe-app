"""Sweep-imports every backend.knowledge.* sub-module.

Catches import-time regressions in lifted code without needing per-module tests.
The lift is large enough that a single missed rename can mask itself behind
lazy imports inside functions — this gate forces every module's surface.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def _iter_modules() -> list[str]:
    names: list[str] = []
    for pkg_name in (
        "backend.knowledge._internal",
        "backend.knowledge.graph",
        "backend.knowledge.ingest",
        "backend.knowledge.retrieval",
        "backend.knowledge.canonicalization",
        "backend.knowledge.mcp",
    ):
        pkg = importlib.import_module(pkg_name)
        if not hasattr(pkg, "__path__"):
            continue
        for _, name, _ in pkgutil.iter_modules(pkg.__path__, prefix=pkg_name + "."):
            names.append(name)
    return names


@pytest.mark.parametrize("module_name", _iter_modules())
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
