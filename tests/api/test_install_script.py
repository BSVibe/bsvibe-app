"""``GET /install-worker.sh`` serves the worker installer for ``curl | sh`` (게이트 2)."""

from __future__ import annotations

import httpx
import pytest

from backend.api.main import create_app

pytestmark = pytest.mark.asyncio


async def _client() -> httpx.AsyncClient:
    app = create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_install_script_is_served_as_shell_no_auth() -> None:
    async with await _client() as c:
        r = await c.get("/install-worker.sh")  # NO auth header
    assert r.status_code == 200, r.text
    assert "shellscript" in r.headers["content-type"]
    body = r.text
    # It is a POSIX shell script that installs the worker CLIs.
    assert body.startswith("#!/bin/sh")
    assert "bsvibe-worker" in body
    assert "uv sync" in body
    # It carries no secret — registration authenticates interactively afterwards.
    assert "worker.token" not in body.split("bsvibe login")[0] or "login" in body


async def test_install_script_mentions_the_run_step_but_no_paste_token() -> None:
    async with await _client() as c:
        body = (await c.get("/install-worker.sh")).text
    assert "bsvibe login" in body
    assert "bsvibe-worker register" in body
    # No legacy install-token paste.
    assert "BSVIBE_WORKER_INSTALL_TOKEN" not in body
