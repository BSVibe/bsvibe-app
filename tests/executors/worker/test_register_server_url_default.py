"""``bsvibe-worker register`` takes its server from ``bsvibe login`` when none is configured.

The ``WorkerSettings.server_url`` default is ``http://localhost:8400``. A host that has just
signed in must not have ``register`` aim at localhost: the credentials' issuer IS the API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from backend.executors.worker import cli as cli_mod
from backend.executors.worker.config import WorkerSettings
from backend.executors.worker.credentials import HostCredentials


def _creds(issuer: str | None) -> HostCredentials:
    return HostCredentials(access_token="ACC", refresh_token=None, expires_at=None, issuer=issuer)


def _run_register(
    tmp_path: Path, *, issuer: str | None, configured: str | None, handler: Any
) -> tuple[int, dict[str, Any]]:
    seen: dict[str, Any] = {}
    settings = WorkerSettings(server_url=configured) if configured else WorkerSettings()
    real_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        seen["base_url"] = str(kwargs.get("base_url"))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    def _save_config(config: Any) -> Path:
        seen["config_server"] = config.server_url
        return tmp_path / "config.json"

    with (
        patch.object(cli_mod, "get_worker_settings", lambda: settings),
        patch.object(cli_mod, "load_host_credentials", lambda: _creds(issuer)),
        patch.object(cli_mod, "save_worker_token", lambda token: tmp_path / "worker.token"),
        patch.object(cli_mod, "save_worker_config", _save_config),
        patch.object(cli_mod.httpx, "AsyncClient", _client_factory),
    ):
        rc = cli_mod.run_bsvibe_worker_cli(
            ["register", "--name", "mac", "--capabilities", "claude_code"]
        )
    return rc, seen


def _created(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"id": "00000000-0000-0000-0000-000000000001", "token": "T"})


def test_unset_server_url_falls_back_to_login_issuer(tmp_path: Path) -> None:
    rc, seen = _run_register(
        tmp_path, issuer="https://api.example.test/", configured=None, handler=_created
    )
    assert rc == 0
    assert seen["base_url"] == "https://api.example.test"
    assert seen["config_server"] == "https://api.example.test"


def test_explicit_server_url_beats_issuer(tmp_path: Path) -> None:
    rc, seen = _run_register(
        tmp_path,
        issuer="https://api.example.test",
        configured="https://other.example.test",
        handler=_created,
    )
    assert rc == 0
    assert seen["base_url"] == "https://other.example.test"


def test_no_issuer_keeps_the_default(tmp_path: Path) -> None:
    rc, seen = _run_register(tmp_path, issuer=None, configured=None, handler=_created)
    assert rc == 0
    assert seen["base_url"] == WorkerSettings.model_fields["server_url"].default


def test_conflict_is_reported_with_the_server_message(tmp_path: Path, capsys: Any) -> None:
    def _conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="revoke the old worker first")

    rc, _ = _run_register(
        tmp_path, issuer="https://api.example.test", configured=None, handler=_conflict
    )
    assert rc == 1
    assert "revoke the old worker first" in capsys.readouterr().err
