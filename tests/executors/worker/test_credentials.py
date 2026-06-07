"""Tests for :mod:`backend.executors.worker.credentials` — Lift E4.

The credentials module is the bridge between ``bsvibe login`` (writes the
host OAuth file) and ``bsvibe-worker register`` (reads it to authenticate).
We assert: round-trip save/load, env fallback, mode 0600, idempotent
clear.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from backend.executors.worker.credentials import (
    CredentialsNotFound,
    HostCredentials,
    WorkerConfig,
    clear_host_credentials,
    clear_worker_config,
    clear_worker_token,
    default_worker_config_path,
    load_host_credentials,
    load_worker_config,
    load_worker_token,
    save_host_credentials,
    save_worker_config,
    save_worker_token,
)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    creds = HostCredentials(
        access_token="ACC", refresh_token="REF", expires_at=1234, issuer="https://x"
    )
    path = tmp_path / "creds.json"
    save_host_credentials(creds, path=path)
    loaded = load_host_credentials(path=path)
    assert loaded == creds


def test_save_sets_owner_only_permissions(tmp_path: Path) -> None:
    """Mode 0600 — POSIX only; the credentials file is a capability."""
    if os.name != "posix":
        pytest.skip("POSIX-only file mode check")
    path = tmp_path / "creds.json"
    save_host_credentials(
        HostCredentials(access_token="A", refresh_token=None, expires_at=None, issuer=None),
        path=path,
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_load_falls_back_to_env_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BSVIBE_ACCESS_TOKEN", "ENV-ONLY-TOKEN")
    loaded = load_host_credentials(path=tmp_path / "absent.json")
    assert loaded.access_token == "ENV-ONLY-TOKEN"
    assert loaded.refresh_token is None


def test_load_raises_when_neither_file_nor_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BSVIBE_ACCESS_TOKEN", raising=False)
    with pytest.raises(CredentialsNotFound):
        load_host_credentials(path=tmp_path / "absent.json")


def test_load_raises_on_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CredentialsNotFound):
        load_host_credentials(path=path)


def test_clear_host_credentials_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    assert clear_host_credentials(path=path) is False  # already gone
    save_host_credentials(
        HostCredentials(access_token="A", refresh_token=None, expires_at=None, issuer=None),
        path=path,
    )
    assert clear_host_credentials(path=path) is True
    assert clear_host_credentials(path=path) is False  # gone again


def test_worker_token_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "worker.token"
    save_worker_token("WORKER-XYZ", path=path)
    assert load_worker_token(path=path) == "WORKER-XYZ"
    assert clear_worker_token(path=path) is True
    assert load_worker_token(path=path) is None


def test_worker_token_strips_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "worker.token"
    save_worker_token("WORKER", path=path)
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert load_worker_token(path=path) == "WORKER"


def test_save_omits_optional_fields_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    save_host_credentials(
        HostCredentials(access_token="A", refresh_token=None, expires_at=None, issuer=None),
        path=path,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"access_token": "A"}


# ── WorkerConfig — Lift E12 ──────────────────────────────────────────────────


def test_worker_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = WorkerConfig(
        name="founder-mac",
        capabilities=["codex", "opencode"],
        labels=["primary"],
        server_url="https://api.bsvibe.dev",
        saved_at=1717900000,
    )
    save_worker_config(cfg, path=path)
    loaded = load_worker_config(path=path)
    assert loaded == cfg


def test_worker_config_save_writes_expected_shape(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = WorkerConfig(
        name="founder-mac",
        capabilities=["codex", "opencode"],
        labels=[],
        server_url="https://api.bsvibe.dev",
        saved_at=1717900000,
    )
    save_worker_config(cfg, path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "name": "founder-mac",
        "capabilities": ["codex", "opencode"],
        "labels": [],
        "server_url": "https://api.bsvibe.dev",
        "saved_at": 1717900000,
    }


def test_worker_config_save_sets_owner_only_permissions(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only file mode check")
    path = tmp_path / "config.json"
    cfg = WorkerConfig(
        name="x",
        capabilities=[],
        labels=[],
        server_url="https://x",
        saved_at=1,
    )
    save_worker_config(cfg, path=path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_worker_config_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_worker_config(path=tmp_path / "absent.json") is None


def test_worker_config_load_returns_none_on_corrupt_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{ not json", encoding="utf-8")
    # Corrupt file = visible warning + fall through to None (callers default).
    loaded = load_worker_config(path=path)
    assert loaded is None


def test_worker_config_load_returns_none_on_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.json"
    path.write_text('{"name": "x"}', encoding="utf-8")
    assert load_worker_config(path=path) is None


def test_clear_worker_config_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    assert clear_worker_config(path=path) is False
    save_worker_config(
        WorkerConfig(
            name="x",
            capabilities=[],
            labels=[],
            server_url="https://x",
            saved_at=1,
        ),
        path=path,
    )
    assert clear_worker_config(path=path) is True
    assert clear_worker_config(path=path) is False


def test_default_worker_config_path_honours_bsvibe_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BSVIBE_HOME", str(tmp_path))
    expected = tmp_path / "config.json"
    assert default_worker_config_path() == expected
