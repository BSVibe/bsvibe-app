"""Shared fixtures for bsvibe-core tests.

Each test must run with a clean environment so that ``BaseSettings``
auto-loading does not pick up stray ``BSVIBE_*`` values from the dev
shell. ``monkeypatch.delenv`` per-test is too verbose; we wipe known
prefixes on every test via an autouse fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

_PREFIXES = ("BSVIBE_", "CORS_", "TEST_")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if any(key.startswith(prefix) for prefix in _PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield
