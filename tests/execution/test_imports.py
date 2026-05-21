"""Sweep-imports every backend.execution.* sub-module.

Bundle X lift is large + heavily stubbed; this gate catches any future change
that breaks module import (typo, dangling reference, etc.). Per-API tests
live under the relevant sub-package once Bundle X integration wires the
abstract surfaces.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def _iter_modules() -> list[str]:
    names: list[str] = []
    pkg = importlib.import_module("backend.execution")
    for _, name, _ in pkgutil.walk_packages(pkg.__path__, prefix="backend.execution."):
        names.append(name)
    return names


@pytest.mark.parametrize("module_name", _iter_modules())
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
