"""``bsvibe login`` — PKCE loopback OAuth flow for the worker host (Lift E4).

Implements RFC 7636 (PKCE) + RFC 8252 (loopback redirect) against the
embedded BSVibe OAuth server (Lift D1). On success, persists the access /
refresh tokens via :mod:`backend.executors.worker.credentials` so the
subsequent ``bsvibe-worker register`` command can authenticate.

Flow:

1. Generate PKCE ``code_verifier`` + ``code_challenge``.
2. Bind a loopback socket on ``127.0.0.1:0`` so the OS picks a free port.
3. Build the authorization URL pointing at the BSVibe OAuth server's
   ``/api/oauth/authorize`` (with ``redirect_uri = http://127.0.0.1:<port>``).
4. Open the URL in the founder's browser (``webbrowser.open``) and serve a
   single GET to ``/`` from a one-shot HTTP server. The browser hits the PWA
   consent screen, the founder approves, the PWA navigates back to the
   loopback URI carrying ``?code=&state=``.
5. Exchange the code at ``/api/oauth/token`` (PKCE proof) for the token pair.
6. Persist into ``~/.config/bsvibe/credentials.json``.

A device-flow alternative is intentionally out of scope for v1 — the embedded
OAuth server (Lift D1) does not yet implement RFC 8628, and the loopback
flow works for every modern OS the founder targets (macOS / Linux / Windows
all ship a default browser).

The CLI surface is :func:`run_login` / :func:`run_logout`, invoked by
``backend.executors.worker.cli.main`` (``bsvibe login`` / ``bsvibe logout``).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import structlog

from backend.executors.worker.credentials import HostCredentials, save_host_credentials

logger = structlog.get_logger(__name__)

# Anonymous DCR is the safe path here — we never want a static client_id
# embedded in the CLI that a fork could hijack. The DCR endpoint
# (POST /api/oauth/register) issues a public-client row bound to the exact
# loopback redirect_uri we just allocated. The DCR client name is purely
# cosmetic — the founder sees it in the consent screen.
_DCR_CLIENT_NAME = "BSVibe Worker CLI"
_DCR_SCOPES = "mcp:read mcp:write mcp:admin"
_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_LOGIN_TIMEOUT_S = 300.0


class LoginError(Exception):
    """Raised when the PKCE login flow fails."""


@dataclass(frozen=True)
class LoginResult:
    """The successful outcome of a login flow — tokens + the issuer URL."""

    credentials: HostCredentials


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for an S256 PKCE flow."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _pick_loopback_port() -> int:
    """Bind ``127.0.0.1:0`` once to discover a free port the AS will allow."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_LOOPBACK_HOST, 0))
        port: int = s.getsockname()[1]
    return port


def _register_dcr_client(*, issuer: str, redirect_uri: str, client: httpx.Client) -> str:
    """RFC 7591 anonymous DCR — return the issued ``client_id``."""
    body: dict[str, Any] = {
        "client_name": _DCR_CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    res = client.post(f"{issuer}/api/oauth/register", json=body, timeout=30.0)
    if res.status_code >= 400:
        raise LoginError(f"DCR failed: {res.status_code} {res.text}")
    payload = res.json()
    cid = payload.get("client_id")
    if not isinstance(cid, str) or not cid:
        raise LoginError(f"DCR returned no client_id: {payload}")
    return cid


def _build_authorize_url(
    *, issuer: str, client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _DCR_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{issuer}/api/oauth/authorize?{urllib.parse.urlencode(params)}"


def _wait_for_callback(port: int, *, timeout: float) -> dict[str, str]:
    """Run a one-shot HTTP server on ``127.0.0.1:port`` and return the query."""
    captured: dict[str, str] = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            for k, v in qs.items():
                if v:
                    captured[k] = v[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>BSVibe sign-in complete.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Quiet — the CLI is the user-facing surface.
            pass

    server = http.server.HTTPServer((_LOOPBACK_HOST, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        done.wait(timeout=timeout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    if not captured:
        raise LoginError(f"timed out waiting for OAuth callback after {timeout:.0f}s")
    if "error" in captured:
        raise LoginError(
            f"OAuth error: {captured['error']} — {captured.get('error_description', '')}"
        )
    if "code" not in captured:
        raise LoginError(f"OAuth callback missing code: {captured}")
    return captured


def _exchange_code(
    *,
    issuer: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    client: httpx.Client,
) -> dict[str, Any]:
    res = client.post(
        f"{issuer}/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=30.0,
    )
    if res.status_code >= 400:
        raise LoginError(f"token exchange failed: {res.status_code} {res.text}")
    payload = res.json()
    if "access_token" not in payload:
        raise LoginError(f"token exchange returned no access_token: {payload}")
    return payload  # type: ignore[no-any-return]


def perform_login(
    *,
    issuer: str,
    open_browser: Callable[[str], bool] | None = None,
    httpx_client: httpx.Client | None = None,
    pick_port: Callable[[], int] | None = None,
    wait_for_callback: Callable[[int, float], dict[str, str]] | None = None,
    timeout_s: float = _DEFAULT_LOGIN_TIMEOUT_S,
) -> LoginResult:
    """Run the full PKCE loopback flow and return :class:`LoginResult`.

    All side-effects (browser launch, port pick, callback HTTP server,
    upstream HTTP) are injectable so tests can exercise the orchestration
    end-to-end without a real browser or network.
    """
    pick_port_fn = pick_port or _pick_loopback_port
    wait_fn = wait_for_callback or (lambda p, t: _wait_for_callback(p, timeout=t))
    open_fn = open_browser or webbrowser.open

    own_client = httpx_client is None
    client = httpx_client if httpx_client is not None else httpx.Client()
    try:
        port = pick_port_fn()
        redirect_uri = f"http://{_LOOPBACK_HOST}:{port}/"
        client_id = _register_dcr_client(issuer=issuer, redirect_uri=redirect_uri, client=client)
        verifier, challenge = make_pkce_pair()
        state = secrets.token_urlsafe(16)
        authorize_url = _build_authorize_url(
            issuer=issuer,
            client_id=client_id,
            redirect_uri=redirect_uri,
            challenge=challenge,
            state=state,
        )
        opened = open_fn(authorize_url)
        if not opened:
            logger.warning("login_browser_open_failed", url=authorize_url)
        captured = wait_fn(port, timeout_s)
        if captured.get("state") != state:
            raise LoginError("state mismatch — possible CSRF; aborting")
        payload = _exchange_code(
            issuer=issuer,
            client_id=client_id,
            code=captured["code"],
            redirect_uri=redirect_uri,
            verifier=verifier,
            client=client,
        )
    finally:
        if own_client:
            client.close()

    expires_in = int(payload.get("expires_in") or 0)
    expires_at = int(time.time()) + expires_in if expires_in else None
    creds = HostCredentials(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
        issuer=issuer,
    )
    return LoginResult(credentials=creds)


def run_login(*, issuer: str) -> LoginResult:
    """Top-level entry point — perform login + persist credentials."""
    result = perform_login(issuer=issuer)
    save_host_credentials(result.credentials)
    return result


__all__ = [
    "LoginError",
    "LoginResult",
    "make_pkce_pair",
    "perform_login",
    "run_login",
]
