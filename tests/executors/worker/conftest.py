"""Subpackage-wide HOME redirect for the worker test suite (Lift E8 Bug 3).

The worker's credential layer (``backend.executors.worker.credentials``) writes
two real files on the host:

* ``~/.bsvibe/worker.token`` — what ``save_worker_token`` writes (mode 0600).
* ``~/.config/bsvibe/credentials.json`` — what ``save_host_credentials`` writes.

Some worker tests (e.g. ``test_persist_writes_key_that_settings_reads``)
exercise ``_persist_worker_token`` which calls ``save_worker_token`` WITHOUT
passing an explicit ``path=``. With no override the default lands at the real
``~/.bsvibe/worker.token`` and overwrites the founder's actual worker token —
exactly what happened during the qazasa123 dogfood that surfaced E8.

This autouse fixture redirects both override env vars
(``BSVIBE_HOME`` + ``XDG_CONFIG_HOME``) to a per-test tmp dir for the whole
``tests/executors/worker/`` subpackage so any current OR future test that
defaults to a home-dir path lands under tmp instead. Tests that intentionally
target a specific path still pass an explicit ``path=`` — the fixture only
shields the default-resolution path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _worker_home_isolation(tmp_path: Path, monkeypatch: Any) -> Iterator[Path]:
    """Redirect ``BSVIBE_HOME`` + ``XDG_CONFIG_HOME`` to a tmp dir for every test.

    A test that calls e.g. ``save_worker_token(token)`` with no explicit
    ``path=`` will write to ``<tmp>/worker.token`` instead of the real
    ``~/.bsvibe/worker.token``. Same for ``save_host_credentials`` and
    ``~/.config/bsvibe/credentials.json``.
    """
    bsvibe_home = tmp_path / "bsvibe-home"
    xdg_config = tmp_path / "xdg-config"
    bsvibe_home.mkdir(parents=True, exist_ok=True)
    xdg_config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BSVIBE_HOME", str(bsvibe_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    yield tmp_path
