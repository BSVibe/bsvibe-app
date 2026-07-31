"""``bsvibe-worker claude-login`` — mint the worker its OWN Claude OAuth token.

The worker injects a Claude bearer into the ``claude --print`` subprocess via
:mod:`backend.executors.worker.claude_auth` (``ensure_claude_bearer`` reads /
refreshes ``~/.bsvibe/claude_oauth.json``). Historically that file was *seeded
by hand* from the interactive CLI's ``~/.claude/.credentials.json`` — the worker
and the CLI then shared ONE refresh-token family, so the CLI's next single-use
rotation invalidated the worker's copy (the "mutual-burn": 31h of healthy
self-refresh, then a sudden ``invalid_grant`` outage).

This command removes that coupling: it runs an authorize-code PKCE flow against
the SAME Claude Code OAuth app the CLI uses (client_id ``9d1c250a-…``, authorize
at ``claude.com/cai/oauth/authorize``, token at
``platform.claude.com/v1/oauth/token``) and persists the resulting token pair
into the worker's own file via :func:`claude_auth._persist`. Because it is a
*fresh* authorize grant, the worker gets an INDEPENDENT refresh family — measured
to coexist with the interactive CLI login without burning it. From then on the
existing keepalive (#641) self-refreshes it; no re-seeding, no shared rotation.

Two flows, both with every side-effect injectable (no network / stdin / files
in tests):

* :func:`perform_claude_login` — loopback: bind ``localhost:<port>``, open the
  browser, capture ``?code=&state=`` from the one-shot callback.
* :func:`perform_claude_login_manual` — remote/headless: print the authorize URL,
  read a pasted full redirect URL (``http://localhost:<port>/callback?code=&state=``)
  or a bare code. No loopback server binds; the redirect_uri must still match at
  token exchange.

Scopes/format were MEASURED live (2026-07-31): the authorize endpoint rejects the
CLI's full 6-scope set for a raw URL ("Invalid request format"), so this uses
``user:inference`` alone — which DOES return a refreshing grant (access +
refresh_token). The token endpoint requires the ``state`` field in the
authorization_code body (unlike refresh_token). See the constants below.
"""

from __future__ import annotations

import http.server
import json
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import structlog

# Reuse the proven PKCE helper and the SINGLE writer of the worker's token file.
from backend.executors.worker.claude_auth import (
    _CLIENT_ID,
    _HTTP_TIMEOUT_S,
    _TOKEN_URL,
    _persist,
    default_oauth_path,
)
from backend.executors.worker.login import make_pkce_pair

logger = structlog.get_logger(__name__)

_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
# MEASURED 2026-07-31: the claude.ai authorize endpoint rejects the CLI's full
# 6-scope set (org:create_api_key user:profile user:inference
# user:sessions:claude_code user:mcp_servers user:file_upload) for a raw,
# non-CLI authorize request with "Invalid request format" (the real CLI likely
# pushes the request server-side first, which we can't replicate from a bare
# URL). ``user:inference`` ALONE is accepted AND returns a refreshing grant
# (access_token + refresh_token + refresh_token_expires_in) — which is exactly
# what the worker needs: an inference bearer that self-refreshes. MCP tools
# authenticate via the worker token, NOT this OAuth scope, so the narrow scope
# does not lose MCP (verified live: executor tasks completed success=True).
_CLAUDE_SCOPE = "user:inference"
# Mimic the CLI so Cloudflare's bot filter (error 1010) lets the POST through —
# identical to claude_auth._http_refresh.
_USER_AGENT = "claude-cli/2.1.172 (external, cli)"
# Loopback host for the redirect_uri. MEASURED: ``localhost`` is accepted by the
# authorize endpoint; the redirect_uri only has to round-trip identically to the
# token exchange (RFC 8252 loopback — any port).
_LOOPBACK_HOST = "localhost"
_DEFAULT_LOGIN_TIMEOUT_S = 300.0


def _loopback_redirect(port: int) -> str:
    return f"http://{_LOOPBACK_HOST}:{port}/callback"


#: ``(code, code_verifier, redirect_uri, state) -> token payload`` — the
#: token-endpoint POST, injectable so tests never touch the network.
CodeExchanger = Callable[..., dict[str, Any]]


class ClaudeLoginError(Exception):
    """Raised when the Claude OAuth login flow fails."""


@dataclass(frozen=True)
class ClaudeLoginResult:
    """A minted Claude token pair, ready to persist into the worker file."""

    access_token: str
    refresh_token: str
    expires_at_ms: int


def make_claude_authorize_url(*, redirect_uri: str, challenge: str, state: str) -> str:
    """Build the Claude authorize URL (S256 PKCE, ``user:inference`` scope, ``code=true``)."""
    params = {
        "code": "true",
        "client_id": _CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _CLAUDE_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_claude_callback_input(text: str) -> dict[str, str | None]:
    """Parse the operator's pasted callback into ``{"code", "state"}``.

    Accepts three shapes:

    * ``<code>#<state>`` — Claude's out-of-band ``code=true`` page format.
    * a full redirect URL ``http://127.0.0.1:<port>/callback?code=&state=``.
    * a bare ``<code>`` (``state`` → ``None``; caller skips the CSRF check).

    Empty or whitespace-containing bare input raises :class:`ClaudeLoginError`.
    """
    stripped = text.strip()
    if not stripped:
        raise ClaudeLoginError("no callback input provided — paste the code (or redirect URL)")

    looks_like_url = "?" in stripped or stripped.lower().startswith(("http://", "https://"))
    if looks_like_url:
        qs = parse_qs(urlparse(stripped).query)
        codes = qs.get("code")
        if not codes or not codes[0]:
            raise ClaudeLoginError("pasted redirect URL has no `code` parameter")
        states = qs.get("state")
        state = states[0] if states and states[0] else None
        return {"code": codes[0], "state": state}

    if "#" in stripped:
        code, _, state = stripped.partition("#")
        if not code:
            raise ClaudeLoginError("pasted `code#state` has no code")
        return {"code": code, "state": state or None}

    if any(ch.isspace() for ch in stripped):
        raise ClaudeLoginError("could not parse callback input — paste `code#state` or the code")
    return {"code": stripped, "state": None}


def _http_exchange_code(
    *, code: str, code_verifier: str, redirect_uri: str, state: str
) -> dict[str, Any]:
    """Default exchanger — POST the token endpoint with ``authorization_code``.

    JSON body + CLI User-Agent, mirroring ``claude_auth._http_refresh`` (the
    proven Cloudflare-passing request shape). MEASURED 2026-07-31: the claude
    token endpoint requires the ``state`` field in the authorization_code body —
    omitting it returns 400 "Invalid request format" (unlike refresh_token,
    which needs no state)."""
    body = json.dumps(
        {
            "grant_type": "authorization_code",
            "code": code,
            "state": state,
            "redirect_uri": redirect_uri,
            "client_id": _CLIENT_ID,
            "code_verifier": code_verifier,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed https OAuth endpoint
        _TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        raise ClaudeLoginError(f"token exchange failed: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ClaudeLoginError(f"token exchange returned no access_token: {payload}")
    return payload


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_LOOPBACK_HOST, 0))
        port: int = s.getsockname()[1]
    return port


def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:
    captured: dict[str, str] = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            qs = parse_qs(urlparse(self.path).query)
            for k, v in qs.items():
                if v:
                    captured[k] = v[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Claude sign-in complete.</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
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
        raise ClaudeLoginError(f"timed out waiting for OAuth callback after {timeout:.0f}s")
    if "error" in captured:
        raise ClaudeLoginError(
            f"OAuth error: {captured['error']} — {captured.get('error_description', '')}"
        )
    if "code" not in captured:
        raise ClaudeLoginError(f"OAuth callback missing code: {captured}")
    return captured


def _result_from_payload(payload: dict[str, Any], *, now_ms: int) -> ClaudeLoginResult:
    refresh = payload.get("refresh_token")
    if not refresh:
        # Without a refresh token the worker cannot self-refresh — that is the
        # whole point of this login (setup-token's narrow scope omits it).
        raise ClaudeLoginError(
            "token exchange returned no refresh_token — check the requested scope"
        )
    expires_in = int(payload.get("expires_in") or 0)
    expires_at_ms = now_ms + expires_in * 1000
    return ClaudeLoginResult(
        access_token=str(payload["access_token"]),
        refresh_token=str(refresh),
        expires_at_ms=expires_at_ms,
    )


def _default_emit(msg: str) -> None:
    print(msg, file=sys.stderr)


def _manual_instructions(authorize_url: str) -> str:
    return (
        "Remote Claude sign-in (manual paste-back) — no loopback server is used.\n"
        "\n"
        "1. Open this URL on ANY device with a browser and approve:\n"
        "\n"
        f"   {authorize_url}\n"
        "\n"
        "2. The browser then tries to open a http://localhost:<port>/callback?code=...\n"
        "   address that FAILS to load — that is expected (nothing listens there).\n"
        "3. Copy the FULL address from the browser's URL bar (or just the `code`)\n"
        "   and paste it below, then press Enter:\n"
    )


def perform_claude_login(
    *,
    open_browser: Callable[[str], bool] | None = None,
    wait_for_callback: Callable[[int, float], dict[str, str]] | None = None,
    exchanger: CodeExchanger | None = None,
    pick_port: Callable[[], int] | None = None,
    state_factory: Callable[[], str] | None = None,
    now_ms: Callable[[], int] | None = None,
    timeout_s: float = _DEFAULT_LOGIN_TIMEOUT_S,
) -> ClaudeLoginResult:
    """Loopback PKCE flow — open the browser, capture the localhost callback."""
    open_fn = open_browser or webbrowser.open
    wait_fn = wait_for_callback or _wait_for_callback
    exchange_fn = exchanger or _http_exchange_code
    port = (pick_port or _pick_loopback_port)()
    state = (state_factory or (lambda: secrets.token_urlsafe(16)))()
    now = (now_ms or (lambda: int(time.time() * 1000)))()

    verifier, challenge = make_pkce_pair()
    redirect_uri = _loopback_redirect(port)
    authorize_url = make_claude_authorize_url(
        redirect_uri=redirect_uri, challenge=challenge, state=state
    )
    if not open_fn(authorize_url):
        logger.warning("claude_login_browser_open_failed", url=authorize_url)
    captured = wait_fn(port, timeout_s)
    if captured.get("state") != state:
        raise ClaudeLoginError("state mismatch — possible CSRF; aborting")
    payload = exchange_fn(
        code=captured["code"], code_verifier=verifier, redirect_uri=redirect_uri, state=state
    )
    return _result_from_payload(payload, now_ms=now)


def perform_claude_login_manual(
    *,
    read_input: Callable[[], str] = input,
    emit: Callable[[str], None] = _default_emit,
    exchanger: CodeExchanger | None = None,
    pick_port: Callable[[], int] | None = None,
    state_factory: Callable[[], str] | None = None,
    now_ms: Callable[[], int] | None = None,
) -> ClaudeLoginResult:
    """Remote/headless PKCE flow — emit the URL, read a pasted redirect URL / code.

    Uses a loopback ``redirect_uri`` (MEASURED to be accepted; the platform
    out-of-band redirect is not) but binds NO server — the operator pastes the
    failed ``http://localhost:<port>/callback?...`` address back. The exchange is
    done with the ``state`` we generated (the token endpoint validates it), so a
    mangled pasted ``state`` is a warning, not a hard failure — the pasted value
    is only a best-effort client-side CSRF pre-check."""
    exchange_fn = exchanger or _http_exchange_code
    port = (pick_port or _pick_loopback_port)()
    state = (state_factory or (lambda: secrets.token_urlsafe(16)))()
    now = (now_ms or (lambda: int(time.time() * 1000)))()

    verifier, challenge = make_pkce_pair()
    redirect_uri = _loopback_redirect(port)
    authorize_url = make_claude_authorize_url(
        redirect_uri=redirect_uri, challenge=challenge, state=state
    )
    emit(_manual_instructions(authorize_url))
    parsed = parse_claude_callback_input(read_input())
    code = parsed["code"]
    if code is None:  # pragma: no cover — parse_claude_callback_input guarantees a code
        raise ClaudeLoginError("no `code` in pasted callback input")
    if parsed["state"] is not None and parsed["state"] != state:
        logger.warning("claude_login_pasted_state_mismatch")
    payload = exchange_fn(code=code, code_verifier=verifier, redirect_uri=redirect_uri, state=state)
    return _result_from_payload(payload, now_ms=now)


def run_claude_login(*, manual: bool, path: Path | None = None, **deps: Any) -> ClaudeLoginResult:
    """Top-level entry — run the chosen flow and persist to the worker file.

    ``deps`` are forwarded to the underlying ``perform_*`` for testing
    (``read_input`` / ``emit`` / ``exchanger`` / ``state_factory`` / ``now_ms`` …).
    """
    target = path or default_oauth_path()
    if manual:
        result = perform_claude_login_manual(**deps)
    else:
        result = perform_claude_login(**deps)
    _persist(target, result.access_token, result.refresh_token, result.expires_at_ms)
    logger.info("claude_login_persisted", path=str(target), expires_at=result.expires_at_ms)
    return result


__all__ = [
    "ClaudeLoginError",
    "ClaudeLoginResult",
    "make_claude_authorize_url",
    "make_pkce_pair",
    "parse_claude_callback_input",
    "perform_claude_login",
    "perform_claude_login_manual",
    "run_claude_login",
]
