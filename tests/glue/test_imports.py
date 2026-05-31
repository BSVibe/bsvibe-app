"""Sweep-imports every backend.{schedule,workflow,workers}.* module.

Per Lift H3b, ``backend.delivery`` is absorbed into ``backend.workflow`` —
the delivery submodules are walked transitively via the workflow root.
Per Lift Schedule, ``backend.intake`` is gone — its M1 carry-overs moved
to the new ``backend.schedule`` bounded context.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def _iter_modules() -> list[str]:
    names: list[str] = []
    for pkg_name in (
        "backend.schedule",
        "backend.workflow",
        "backend.workers",
    ):
        pkg = importlib.import_module(pkg_name)
        if not hasattr(pkg, "__path__"):
            continue
        for _, name, _ in pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):
            names.append(name)
    return names


@pytest.mark.parametrize("module_name", _iter_modules())
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
