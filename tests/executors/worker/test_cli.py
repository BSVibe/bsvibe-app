"""Tests for :mod:`backend.executors.worker.cli` — Lift E4.

The CLI dispatcher is a thin argparse front-end around login / register /
logout. We exercise the parser shape + the cmd functions with stubbed
dependencies so the orchestration is covered without hitting the network /
real browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker.credentials import HostCredentials
from backend.executors.worker.login import LoginResult


def test_build_bsvibe_parser_lists_subcommands() -> None:
    parser = cli_mod.build_bsvibe_parser()
    help_text = parser.format_help()
    for cmd in ("login", "logout", "status"):
        assert cmd in help_text


def test_build_bsvibe_worker_parser_lists_subcommands() -> None:
    parser = cli_mod.build_bsvibe_worker_parser()
    help_text = parser.format_help()
    for cmd in ("register", "run", "logout"):
        assert cmd in help_text


def test_login_writes_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred_path = tmp_path / "creds.json"
    monkeypatch.setattr(cli_mod, "default_credentials_path", lambda: cred_path, raising=False)

    def _fake_run_login(*, issuer: str) -> LoginResult:
        # The real run_login also persists — emulate that explicitly here so
        # the cmd's print path runs but file IO is deterministic.
        from backend.executors.worker.credentials import save_host_credentials

        creds = HostCredentials(access_token="A", refresh_token="R", expires_at=None, issuer=issuer)
        save_host_credentials(creds, path=cred_path)
        return LoginResult(credentials=creds)

    monkeypatch.setattr(cli_mod, "run_login", _fake_run_login)

    rc = cli_mod.run_bsvibe_cli(["login", "--issuer", "https://auth.test"])
    assert rc == 0
    payload = json.loads(cred_path.read_text(encoding="utf-8"))
    assert payload["access_token"] == "A"
    assert payload["issuer"] == "https://auth.test"


def test_login_returns_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.executors.worker.login import LoginError

    def _fail(*, issuer: str) -> LoginResult:  # noqa: ARG001
        raise LoginError("boom")

    monkeypatch.setattr(cli_mod, "run_login", _fail)
    rc = cli_mod.run_bsvibe_cli(["login"])
    assert rc == 1


def test_logout_clears_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "creds.json"
    cred.write_text('{"access_token": "X"}', encoding="utf-8")
    worker = tmp_path / "worker.token"
    worker.write_text("WT\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "default_credentials_path", lambda: cred)
    monkeypatch.setattr(cli_mod, "default_worker_token_path", lambda: worker)
    # The functions used by _cmd_logout pull defaults via the module-level
    # helpers — patch the actual clear_* functions to take our paths.
    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred, raising=False)
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker, raising=False)

    rc = cli_mod.run_bsvibe_cli(["logout"])
    assert rc == 0
    assert not cred.exists()
    assert not worker.exists()


def test_status_signed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "missing.json"
    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.delenv("BSVIBE_ACCESS_TOKEN", raising=False)
    rc = cli_mod.run_bsvibe_cli(["status"])
    assert rc == 1


def test_status_signed_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred = tmp_path / "creds.json"
    cred.write_text(
        json.dumps({"access_token": "A", "issuer": "https://auth.test", "expires_at": 1234}),
        encoding="utf-8",
    )
    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    rc = cli_mod.run_bsvibe_cli(["status"])
    assert rc == 0


def test_worker_register_calls_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bsvibe-worker register --name X`` should POST /api/v1/workers/register.

    With credentials present, the bearer header is sent and the response's
    worker token is persisted at the worker.token path.
    """
    cred = tmp_path / "creds.json"
    cred.write_text(
        json.dumps({"access_token": "ACC-LIVE", "issuer": "https://auth.test"}),
        encoding="utf-8",
    )
    worker_token = tmp_path / "worker.token"

    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker_token)
    # The CLI reads settings.server_url; force a local URL.
    monkeypatch.setenv("BSVIBE_WORKER_SERVER_URL", "http://test")
    # Reset settings cache so the new env takes effect.
    from backend.executors.worker.config import get_worker_settings

    get_worker_settings.cache_clear()

    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            201, json={"id": "00000000-0000-0000-0000-000000000001", "token": "WK-TOKEN"}
        )

    # Patch AsyncClient construction so the CLI uses our MockTransport.
    real_async_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    with patch.object(cli_mod.httpx, "AsyncClient", _client_factory):
        rc = cli_mod.run_bsvibe_worker_cli(
            ["register", "--name", "mac-mini", "--capabilities", "claude_code"]
        )
    assert rc == 0
    assert captured["path"] == "/api/v1/workers/register"
    assert captured["auth"] == "Bearer ACC-LIVE"
    assert worker_token.read_text(encoding="utf-8").strip() == "WK-TOKEN"


def test_worker_register_fails_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cred = tmp_path / "absent.json"
    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.delenv("BSVIBE_ACCESS_TOKEN", raising=False)

    rc = cli_mod.run_bsvibe_worker_cli(["register", "--name", "mac-mini"])
    assert rc == 1


def test_worker_register_persists_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lift E12 — successful register MUST also write ``~/.bsvibe/config.json``
    with name + capabilities + labels + server_url + saved_at.

    Without this persistence, ``bsvibe-worker run`` from a different CWD has no
    way to recover the founder's ``--name`` / ``--capabilities`` and silently
    auto-re-registers with hostname-detected defaults (the qazasa123 dogfood bug).
    """
    cred = tmp_path / "creds.json"
    cred.write_text(
        json.dumps({"access_token": "ACC", "issuer": "https://auth.test"}),
        encoding="utf-8",
    )
    worker_token = tmp_path / "worker.token"
    worker_config = tmp_path / "config.json"

    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker_token)
    monkeypatch.setattr(cred_mod, "default_worker_config_path", lambda: worker_config)
    monkeypatch.setenv("BSVIBE_WORKER_SERVER_URL", "https://api.bsvibe.dev")

    from backend.executors.worker.config import get_worker_settings

    get_worker_settings.cache_clear()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "00000000-0000-0000-0000-000000000001", "token": "WK"}
        )

    real_async_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    with patch.object(cli_mod.httpx, "AsyncClient", _client_factory):
        rc = cli_mod.run_bsvibe_worker_cli(
            [
                "register",
                "--name",
                "founder-mac",
                "--capabilities",
                "codex,opencode",
                "--labels",
                "primary",
            ]
        )
    assert rc == 0
    assert worker_config.exists()
    payload = json.loads(worker_config.read_text(encoding="utf-8"))
    assert payload["name"] == "founder-mac"
    assert payload["capabilities"] == ["codex", "opencode"]
    assert payload["labels"] == ["primary"]
    assert payload["server_url"] == "https://api.bsvibe.dev"
    assert isinstance(payload["saved_at"], int)
    assert payload["saved_at"] > 0


def test_worker_register_does_not_write_cwd_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lift E12 — the canonical store is ``~/.bsvibe/config.json``; the legacy
    CWD ``.env`` writeback is gone (it caused the qazasa123 cross-CWD bug)."""
    cred = tmp_path / "creds.json"
    cred.write_text(
        json.dumps({"access_token": "ACC", "issuer": "https://auth.test"}),
        encoding="utf-8",
    )
    worker_token = tmp_path / "worker.token"
    worker_config = tmp_path / "config.json"

    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker_token)
    monkeypatch.setattr(cred_mod, "default_worker_config_path", lambda: worker_config)
    monkeypatch.setenv("BSVIBE_WORKER_SERVER_URL", "http://test")
    monkeypatch.chdir(tmp_path)

    from backend.executors.worker.config import get_worker_settings

    get_worker_settings.cache_clear()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "00000000-0000-0000-0000-000000000001", "token": "WK"}
        )

    real_async_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    with patch.object(cli_mod.httpx, "AsyncClient", _client_factory):
        rc = cli_mod.run_bsvibe_worker_cli(
            ["register", "--name", "x", "--capabilities", "claude_code"]
        )
    assert rc == 0
    # The CWD .env file MUST NOT have been created by the CLI.
    assert not (tmp_path / ".env").exists()


def test_worker_status_shows_persisted_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bsvibe-worker status`` prints the persisted config + token presence."""
    from backend.executors.worker import credentials as cred_mod
    from backend.executors.worker.credentials import (
        WorkerConfig,
        save_worker_config,
        save_worker_token,
    )

    worker_token = tmp_path / "worker.token"
    worker_config = tmp_path / "config.json"
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker_token)
    monkeypatch.setattr(cred_mod, "default_worker_config_path", lambda: worker_config)
    monkeypatch.setattr(cli_mod, "default_worker_token_path", lambda: worker_token)
    monkeypatch.setattr(cli_mod, "default_worker_config_path", lambda: worker_config)

    cfg = WorkerConfig(
        name="founder-mac",
        capabilities=["codex", "opencode"],
        labels=[],
        server_url="https://api.bsvibe.dev",
        saved_at=1717900000,
    )
    save_worker_config(cfg, path=worker_config)
    save_worker_token("WORKER-TOK", path=worker_token)

    rc = cli_mod.run_bsvibe_worker_cli(["status"])
    assert rc == 0


def test_worker_status_when_nothing_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: tmp_path / "worker.token")
    monkeypatch.setattr(cred_mod, "default_worker_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(cli_mod, "default_worker_token_path", lambda: tmp_path / "worker.token")
    monkeypatch.setattr(cli_mod, "default_worker_config_path", lambda: tmp_path / "config.json")

    rc = cli_mod.run_bsvibe_worker_cli(["status"])
    # Returns 1 because there's no persisted config/token yet — the founder
    # needs to run `bsvibe-worker register` first.
    assert rc == 1
