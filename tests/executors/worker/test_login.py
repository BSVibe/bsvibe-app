"""Tests for :mod:`backend.executors.worker.login` — Lift E4 PKCE loopback.

The login module orchestrates an interactive PKCE+loopback OAuth flow against
the BSVibe OAuth server. We assert the orchestration by injecting fakes for
every side-effect: a stubbed httpx client (DCR + token exchange), a
deterministic loopback "callback" (returning the code + state), and a no-op
browser open. End-to-end runs without touching the network or stdlib HTTP
server.
"""

from __future__ import annotations

import httpx
import pytest

from backend.executors.worker.login import (
    LoginError,
    make_pkce_pair,
    parse_callback_input,
    perform_login,
    perform_login_manual,
)


def _fake_httpx_client(*, dcr_status: int = 201, token_status: int = 200) -> httpx.Client:
    """Build a Client backed by a MockTransport with DCR + token endpoints."""
    captured: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("calls", []).append(  # type: ignore[union-attr]
            (request.method, request.url.path)
        )
        if request.url.path == "/api/oauth/register":
            import json as _json

            captured["dcr_body"] = _json.loads(request.content.decode("utf-8") or "{}")
            if dcr_status >= 400:
                return httpx.Response(dcr_status, text="DCR rejected")
            return httpx.Response(
                dcr_status,
                json={
                    "client_id": "dcr-fake-123",
                    "client_name": "BSVibe Worker CLI",
                    "redirect_uris": [request.url.host],
                    "allowed_scopes": ["mcp:read", "mcp:write", "mcp:admin"],
                    "created_at": "2026-06-06T00:00:00+00:00",
                },
            )
        if request.url.path == "/api/oauth/token":
            if token_status >= 400:
                return httpx.Response(token_status, text="bad PKCE")
            return httpx.Response(
                token_status,
                json={
                    "access_token": "ACCESS-LIVE",
                    "refresh_token": "REFRESH-LIVE",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp:read mcp:write mcp:admin",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)
    client._captured = captured  # type: ignore[attr-defined] — diagnostics for tests
    return client


def test_make_pkce_pair_returns_url_safe_base64() -> None:
    verifier, challenge = make_pkce_pair()
    assert len(verifier) >= 43  # RFC 7636 §4.1 — 43 char min
    assert len(challenge) >= 43
    # URL-safe alphabet, no padding.
    assert all(c.isalnum() or c in "-_" for c in verifier)
    assert all(c.isalnum() or c in "-_" for c in challenge)


def test_perform_login_happy_path() -> None:
    captured_state: dict[str, str] = {}

    def _pick_port() -> int:
        return 41234

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        # Use the same state the URL builder generated — capture it by reading
        # the side-effect of perform_login's _build_authorize_url indirectly:
        # the URL is opened via open_browser, so we capture state from there.
        return {"code": "CODE-RECEIVED", "state": captured_state["state"]}

    def _open(url: str) -> bool:
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(url).query)
        captured_state["state"] = params["state"][0]
        return True

    client = _fake_httpx_client()
    try:
        result = perform_login(
            issuer="https://auth.test",
            open_browser=_open,
            httpx_client=client,
            pick_port=_pick_port,
            wait_for_callback=_wait_for_callback,
            timeout_s=5.0,
        )
    finally:
        client.close()

    assert result.credentials.access_token == "ACCESS-LIVE"
    assert result.credentials.refresh_token == "REFRESH-LIVE"
    assert result.credentials.issuer == "https://auth.test"
    assert result.credentials.expires_at is not None


def test_perform_login_rejects_state_mismatch() -> None:
    def _pick_port() -> int:
        return 41234

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "CODE", "state": "WRONG-STATE"}

    def _open(url: str) -> bool:  # noqa: ARG001
        return True

    client = _fake_httpx_client()
    try:
        with pytest.raises(LoginError, match="state mismatch"):
            perform_login(
                issuer="https://auth.test",
                open_browser=_open,
                httpx_client=client,
                pick_port=_pick_port,
                wait_for_callback=_wait_for_callback,
                timeout_s=1.0,
            )
    finally:
        client.close()


def test_perform_login_fails_when_dcr_rejects() -> None:
    def _pick_port() -> int:
        return 41234

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "x", "state": "x"}

    def _open(url: str) -> bool:  # noqa: ARG001
        return True

    client = _fake_httpx_client(dcr_status=400)
    try:
        with pytest.raises(LoginError, match="DCR failed"):
            perform_login(
                issuer="https://auth.test",
                open_browser=_open,
                httpx_client=client,
                pick_port=_pick_port,
                wait_for_callback=_wait_for_callback,
                timeout_s=1.0,
            )
    finally:
        client.close()


def test_perform_login_fails_when_token_exchange_rejects() -> None:
    captured: dict[str, str] = {}

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "CODE", "state": captured["state"]}

    def _open(url: str) -> bool:
        from urllib.parse import parse_qs, urlparse

        captured["state"] = parse_qs(urlparse(url).query)["state"][0]
        return True

    client = _fake_httpx_client(token_status=400)
    try:
        with pytest.raises(LoginError, match="token exchange failed"):
            perform_login(
                issuer="https://auth.test",
                open_browser=_open,
                httpx_client=client,
                pick_port=lambda: 41234,
                wait_for_callback=_wait_for_callback,
                timeout_s=1.0,
            )
    finally:
        client.close()


def test_perform_login_dcr_body_requests_full_mcp_scope_set() -> None:
    """Regression — DCR body must include scope=mcp:read mcp:write mcp:admin.

    Without it the anonymous client is registered with only DEFAULT_SCOPE
    (``mcp:read``), and the subsequent /authorize ask for write/admin fails
    with ``invalid_scope`` (see hotfix Lift E6).
    """
    captured: dict[str, str] = {}

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "CODE", "state": captured["state"]}

    def _open(url: str) -> bool:
        from urllib.parse import parse_qs, urlparse

        captured["state"] = parse_qs(urlparse(url).query)["state"][0]
        return True

    client = _fake_httpx_client()
    try:
        perform_login(
            issuer="https://auth.test",
            open_browser=_open,
            httpx_client=client,
            pick_port=lambda: 41234,
            wait_for_callback=_wait_for_callback,
            timeout_s=1.0,
        )
        body = client._captured["dcr_body"]  # type: ignore[attr-defined]
        assert "scope" in body, "DCR body must carry the requested scope set"
        assert body["scope"] == "mcp:read mcp:write mcp:admin"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# parse_callback_input — the out-of-band paste-back parser
# ---------------------------------------------------------------------------
def test_parse_callback_input_full_redirect_url() -> None:
    parsed = parse_callback_input("http://127.0.0.1:41234/?code=CODE-XYZ&state=STATE-ABC")
    assert parsed == {"code": "CODE-XYZ", "state": "STATE-ABC"}


def test_parse_callback_input_bare_code() -> None:
    parsed = parse_callback_input("  CODE-ONLY-123  ")
    assert parsed == {"code": "CODE-ONLY-123", "state": None}


def test_parse_callback_input_ignores_extra_query_params() -> None:
    parsed = parse_callback_input(
        "http://127.0.0.1:41234/?iss=https://a.test&code=C1&state=S1&session_state=zz"
    )
    assert parsed == {"code": "C1", "state": "S1"}


def test_parse_callback_input_url_without_state() -> None:
    parsed = parse_callback_input("http://127.0.0.1:41234/?code=NOSTATE")
    assert parsed == {"code": "NOSTATE", "state": None}


def test_parse_callback_input_empty_raises() -> None:
    with pytest.raises(LoginError):
        parse_callback_input("   ")


def test_parse_callback_input_url_missing_code_raises() -> None:
    with pytest.raises(LoginError, match="code"):
        parse_callback_input("http://127.0.0.1:41234/?state=only")


# ---------------------------------------------------------------------------
# perform_login_manual — the loopback-server-free paste-back flow
# ---------------------------------------------------------------------------
def test_perform_login_manual_happy_path_with_full_url() -> None:
    emitted: list[str] = []
    holder: dict[str, str] = {}

    def _emit(msg: str) -> None:
        # The authorize URL is emitted — capture the generated state from it,
        # mirroring how the loopback tests read state off the opened URL.
        emitted.append(msg)
        from urllib.parse import parse_qs, urlparse

        for line in msg.splitlines():
            tok = line.strip()
            if "/api/oauth/authorize" in tok:
                holder["state"] = parse_qs(urlparse(tok).query)["state"][0]

    def _read_input() -> str:
        return f"http://127.0.0.1:41234/?code=CODE-RECEIVED&state={holder['state']}"

    client = _fake_httpx_client()
    try:
        result = perform_login_manual(
            issuer="https://auth.test",
            read_input=_read_input,
            emit=_emit,
            httpx_client=client,
            pick_port=lambda: 41234,
        )
    finally:
        client.close()

    assert result.credentials.access_token == "ACCESS-LIVE"
    assert result.credentials.refresh_token == "REFRESH-LIVE"
    assert result.credentials.issuer == "https://auth.test"
    # The authorize URL must have been emitted for the user to open.
    assert any("/api/oauth/authorize" in m for m in emitted)
    # The token exchange used the loopback redirect_uri even though no server ran.
    calls = client._captured["calls"]  # type: ignore[attr-defined]
    assert ("POST", "/api/oauth/token") in calls


def test_perform_login_manual_bare_code_skips_state_check() -> None:
    def _emit(msg: str) -> None:  # noqa: ARG001
        return None

    def _read_input() -> str:
        return "BARE-CODE-999"

    client = _fake_httpx_client()
    try:
        result = perform_login_manual(
            issuer="https://auth.test",
            read_input=_read_input,
            emit=_emit,
            httpx_client=client,
            pick_port=lambda: 41234,
        )
    finally:
        client.close()

    assert result.credentials.access_token == "ACCESS-LIVE"
    calls = client._captured["calls"]  # type: ignore[attr-defined]
    assert ("POST", "/api/oauth/token") in calls


def test_perform_login_manual_rejects_state_mismatch_without_exchange() -> None:
    def _emit(msg: str) -> None:  # noqa: ARG001
        return None

    def _read_input() -> str:
        return "http://127.0.0.1:41234/?code=CODE&state=WRONG-STATE"

    client = _fake_httpx_client()
    try:
        with pytest.raises(LoginError, match="state mismatch"):
            perform_login_manual(
                issuer="https://auth.test",
                read_input=_read_input,
                emit=_emit,
                httpx_client=client,
                pick_port=lambda: 41234,
            )
        # No token exchange must have happened on a CSRF mismatch.
        calls = client._captured["calls"]  # type: ignore[attr-defined]
        assert ("POST", "/api/oauth/token") not in calls
    finally:
        client.close()
