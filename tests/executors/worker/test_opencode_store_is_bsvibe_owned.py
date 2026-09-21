"""The worker's opencode store must be BSVibe's, not the founder's (#1016).

opencode keeps its sessions in a SQLite store. The worker daemon and the
founder's interactive ``opencode`` resolved the SAME file
(``~/.local/share/opencode/opencode.db``), so:

* the founder merely STARTING opencode — no deploy, no code change — could stop
  prod. It happened: a 1.17.3 shell migrated the store and the worker's 1.15.12
  then died on every message insert (``NOT NULL constraint failed:
  session_message.seq``), the exact signature ``opencode.py`` had documented;
* two worker daemons on one host bit the same store;
* agent-run sessions landed in the founder's personal history.

**Why ``OPENCODE_DB`` and not ``XDG_DATA_HOME``.** Measured against opencode
1.17.3: both are honoured, but ``XDG_DATA_HOME`` moves the WHOLE data dir —
including ``auth.json``, the provider credential. Splitting the store must not
cost the worker its login. ``OPENCODE_DB`` moves only the database, which is
exactly the thing that broke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.executors.worker import opencode_server
from backend.executors.worker.config import WorkerSettings, default_opencode_db_path

from .test_opencode_server import _FakeProcess, _ok_health_transport, _patch_subprocess

pytestmark = pytest.mark.asyncio

_SHARED = ".local/share/opencode"


async def _spawn_env(monkeypatch: pytest.MonkeyPatch, settings: WorkerSettings) -> dict[str, str]:
    """Start the daemon against a stub and return the env it was spawned with."""
    proc = _FakeProcess(stdout_lines=[b"opencode server listening on http://127.0.0.1:1/\n"])
    spawns = _patch_subprocess(monkeypatch, proc)
    await opencode_server.start_opencode_serve(settings, http_transport=_ok_health_transport())
    env = spawns[0]["kwargs"]["env"]
    assert isinstance(env, dict)
    return env


async def test_the_daemon_is_pinned_to_a_bsvibe_owned_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    env = await _spawn_env(monkeypatch, WorkerSettings(name="mac-mini-e2e"))

    db = env.get("OPENCODE_DB")
    assert db, "the daemon must be told which store to use"
    assert Path(db).is_absolute(), "opencode only takes an absolute OPENCODE_DB verbatim"
    assert str(tmp_path) in db
    assert _SHARED not in db, "that is the founder's interactive store"


async def test_the_store_is_per_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two daemons on one host must not bite the same SQLite file."""
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    a = await _spawn_env(monkeypatch, WorkerSettings(name="mac-mini-e2e"))
    b = await _spawn_env(monkeypatch, WorkerSettings(name="admin-test-exec"))

    assert a["OPENCODE_DB"] != b["OPENCODE_DB"]


async def test_a_worker_name_cannot_escape_the_bsvibe_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The name is operator-supplied (``BSVIBE_WORKER_NAME``) and reaches a path."""
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    env = await _spawn_env(monkeypatch, WorkerSettings(name="../../../etc/evil"))

    assert str(tmp_path) in env["OPENCODE_DB"]
    assert ".." not in Path(env["OPENCODE_DB"]).parts


async def test_an_explicit_setting_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chosen = tmp_path / "elsewhere" / "oc.db"
    env = await _spawn_env(monkeypatch, WorkerSettings(name="w", opencode_db_path=str(chosen)))

    assert env["OPENCODE_DB"] == str(chosen)


async def test_the_provider_credential_is_NOT_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole reason the handle is OPENCODE_DB and not XDG_DATA_HOME.

    ``auth.json`` lives in opencode's XDG data dir. Redirecting that dir would
    split the store AND take the worker's provider login with it — a fix that
    breaks every run. Splitting must be surgical.
    """
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "/host/xdg")
    env = await _spawn_env(monkeypatch, WorkerSettings(name="w"))

    assert env.get("XDG_DATA_HOME") == "/host/xdg", "the daemon's data dir must be untouched"


async def test_the_parent_directory_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """opencode opens the file; nothing creates the tree for it."""
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    env = await _spawn_env(monkeypatch, WorkerSettings(name="w"))

    assert Path(env["OPENCODE_DB"]).parent.is_dir()


# ---------------------------------------------------------------------------
# Corruption recovery must follow the store it actually pinned
# ---------------------------------------------------------------------------


async def test_corruption_recovery_quarantines_the_worker_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recovery path moved ``<xdg>/opencode.db``. Pinning the daemon
    elsewhere without moving this too would quarantine a file nobody uses —
    and leave the broken one in place."""
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    settings = WorkerSettings(name="w")
    db = default_opencode_db_path(settings.name)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"corrupt")

    shared = tmp_path / "shared-xdg" / "opencode"
    shared.mkdir(parents=True)
    (shared / "opencode.db").write_bytes(b"the founder's")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "shared-xdg"))

    # Stub the restart itself: what is under test is WHICH store recovery moves
    # aside, not the daemon handshake (covered in test_opencode_server.py).
    async def _fake_start(_settings, **_kw):
        return opencode_server.OpenCodeServerProcess(url="http://127.0.0.1:1", process=None)

    monkeypatch.setattr(opencode_server, "get_serve_daemon", lambda: None)
    monkeypatch.setattr(opencode_server, "start_opencode_serve", _fake_start)
    await opencode_server.restart_serve_after_corruption(settings)

    assert not db.exists(), "the worker's broken store must be moved aside"
    assert (shared / "opencode.db").read_bytes() == b"the founder's", (
        "recovery must never touch the founder's store"
    )


@pytest.mark.filterwarnings("ignore")
def test_the_default_path_is_not_the_shared_one(tmp_path, monkeypatch) -> None:
    """A guard on the DEFAULT, because an unset setting is the state prod runs in."""
    monkeypatch.delenv("BSVIBE_HOME", raising=False)
    assert _SHARED not in str(default_opencode_db_path("w"))
