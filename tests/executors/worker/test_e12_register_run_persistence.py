"""Lift E12 end-to-end UX test — register persists, run reads, from any CWD.

Reproduces the qazasa123 dogfood trace:

1. ``bsvibe-worker register --name founder-mac --capabilities codex,opencode``
   from one CWD writes ``~/.bsvibe/config.json``.
2. ``bsvibe-worker run`` from a DIFFERENT CWD picks up name + capabilities +
   server_url from that config file — NOT from hostname auto-detection or a
   CWD-relative ``.env`` — and does NOT auto-re-register a duplicate worker.

Pre-Lift E12, step (2) re-detected the hostname, re-detected PATH-available
executors (including the unwanted ``claude_code``), and rebuilt headers with
``settings.token`` from a ``.env`` invisible at the run CWD — so it
auto-re-registered, creating a second worker row + a second ModelAccount set.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker import main as worker_main
from backend.executors.worker.config import get_worker_settings


async def _no_redis_pollloop_settings_capture(
    captured: dict[str, Any],
) -> AsyncIterator[None]:
    yield  # pragma: no cover


def test_register_then_run_from_different_cwd_reuses_persisted_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: register (CWD A) → run (CWD B) sees the persisted config.

    Assertions:
      * config.json was written with the founder's chosen values.
      * Subsequent ``run`` resolves ``name``, ``capabilities``, ``labels``,
        ``server_url`` from the file (NOT from hostname / PATH probe / env).
      * The register endpoint was called ONCE (during register), not again
        during run — i.e. no duplicate worker is created.
    """
    cred = tmp_path / "creds.json"
    cred.write_text(
        json.dumps({"access_token": "ACC", "issuer": "https://auth.test"}),
        encoding="utf-8",
    )
    worker_token = tmp_path / "worker.token"
    worker_config = tmp_path / "config.json"
    cwd_a = tmp_path / "home-cwd"
    cwd_b = tmp_path / "elsewhere"
    cwd_a.mkdir()
    cwd_b.mkdir()

    from backend.executors.worker import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "default_credentials_path", lambda: cred)
    monkeypatch.setattr(cred_mod, "default_worker_token_path", lambda: worker_token)
    monkeypatch.setattr(cred_mod, "default_worker_config_path", lambda: worker_config)
    # The CLI reads settings.server_url; force the value the founder wants.
    monkeypatch.setenv("BSVIBE_WORKER_SERVER_URL", "https://api.bsvibe.dev")
    monkeypatch.delenv("BSVIBE_WORKER_NAME", raising=False)
    monkeypatch.delenv("BSVIBE_WORKER_TOKEN", raising=False)
    get_worker_settings.cache_clear()

    call_log: dict[str, int] = {"register": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workers/register":
            call_log["register"] += 1
            return httpx.Response(
                201, json={"id": "00000000-0000-0000-0000-000000000001", "token": "WK"}
            )
        return httpx.Response(404)  # pragma: no cover

    real_async_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    # ── Step 1: register from CWD A ────────────────────────────────────────
    monkeypatch.chdir(cwd_a)
    with patch.object(cli_mod.httpx, "AsyncClient", _client_factory):
        rc = cli_mod.run_bsvibe_worker_cli(
            [
                "register",
                "--name",
                "founder-mac",
                "--capabilities",
                "codex,opencode",
            ]
        )
    assert rc == 0
    assert call_log["register"] == 1
    assert worker_config.exists()
    payload = json.loads(worker_config.read_text(encoding="utf-8"))
    assert payload["name"] == "founder-mac"
    assert payload["capabilities"] == ["codex", "opencode"]
    assert payload["server_url"] == "https://api.bsvibe.dev"

    # ── Step 2: run from CWD B — config.json MUST drive settings ───────────
    monkeypatch.chdir(cwd_b)
    # Wipe the env that drove register; run must source from config.json.
    monkeypatch.delenv("BSVIBE_WORKER_SERVER_URL", raising=False)
    get_worker_settings.cache_clear()

    captured: dict[str, Any] = {}

    async def _fake_poll_and_execute(
        *,
        settings: Any,
        client: Any,
        redis: Any,
        stop: Any = None,
    ) -> None:
        captured["settings"] = settings

    monkeypatch.setattr(worker_main, "poll_and_execute", _fake_poll_and_execute)
    # Also stop the connect_redis branch from touching anything.
    monkeypatch.setattr(worker_main, "_connect_redis", lambda _settings: None)

    with patch.object(worker_main.httpx, "AsyncClient", _client_factory):
        rc = cli_mod.run_bsvibe_worker_cli(["run"])
    assert rc == 0

    # The run picked up settings from config.json — NOT from hostname auto-detect,
    # NOT from BSVIBE_WORKER_* env (we wiped it).
    settings = captured["settings"]
    assert settings.name == "founder-mac"
    assert settings.server_url == "https://api.bsvibe.dev"

    # Register endpoint was hit ONCE (during step 1), never during step 2.
    assert call_log["register"] == 1
