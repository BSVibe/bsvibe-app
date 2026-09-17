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

import json
import secrets
import sys
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

#: The redirect the real CLI uses — MEASURED 2026-09-17 against `claude
#: setup-token` (v2.1.268), captured over a pty. The platform's out-of-band
#: callback page shows the operator a code to paste back; nothing has to listen
#: anywhere, which is what makes this work over SSH.
#:
#: ⚠️ This REPLACED a loopback redirect that was measured to be accepted on
#: 2026-07-31 — the platform flipped which redirect it takes, and the stale one
#: made the authorize PAGE fail with "Invalid request format" before any token
#: call happened. A measurement has a date; re-measure against the real CLI
#: before assuming this line is still true.
CLAUDE_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

#: Entropy for the authorize ``state``. 32 bytes → 43 urlsafe chars, matching
#: what ``claude setup-token`` sends. The endpoint validates the length: 16
#: bytes (22 chars) is rejected outright — see :func:`_paste_back_login`.
_STATE_BYTES = 32
# Mimic the CLI so Cloudflare's bot filter (error 1010) lets the POST through —
# identical to claude_auth._http_refresh.
_USER_AGENT = "claude-cli/2.1.172 (external, cli)"
# Loopback host for the redirect_uri. MEASURED: ``localhost`` is accepted by the
# authorize endpoint; the redirect_uri only has to round-trip identically to the
# token exchange (RFC 8252 loopback — any port).
_DEFAULT_LOGIN_TIMEOUT_S = 300.0


class ClaudeLoginError(Exception):
    """Raised when the Claude OAuth login flow fails."""


@dataclass(frozen=True)
class ClaudeLoginResult:
    """A minted Claude token pair, ready to persist into the worker file."""

    access_token: str
    refresh_token: str
    expires_at_ms: int


#: ``(code, code_verifier, redirect_uri, state) -> token payload`` — the
#: token-endpoint POST, injectable so tests never touch the network.
CodeExchanger = Callable[..., dict[str, Any]]


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


def _error_body(exc: urllib.error.HTTPError) -> str:
    """The response body of a failed exchange, or a marker when there is none.

    Best-effort by contract: this runs while raising a more useful error, so it
    must never raise one of its own. An unreadable body degrades to a marker —
    "no body" is itself a fact worth printing, and silence here would put us back
    where we started.
    """
    try:
        raw = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 — diagnostics only, never raise from the raiser
        return "<body unreadable>"
    return raw or "<no body>"


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
    except urllib.error.HTTPError as exc:
        # ``str(HTTPError)`` is "HTTP Error 400: Bad Request" and DROPS the body —
        # but the body is the only place the reason lives, and the reasons call
        # for different actions: ``invalid_grant`` (the code was already used or
        # expired — start over), ``invalid_request`` (a mismatched field, almost
        # always the redirect_uri of a DIFFERENT run), ``unauthorized_client``.
        # A founder hit a bare 400 with no way to tell those apart.
        raise ClaudeLoginError(f"token exchange failed: {exc} — {_error_body(exc)}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ClaudeLoginError(f"token exchange failed: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ClaudeLoginError(f"token exchange returned no access_token: {payload}")
    return payload


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
        "Remote Claude sign-in (out-of-band paste-back) — nothing listens locally.\n"
        "\n"
        "1. Open this URL on ANY device with a browser and approve:\n"
        "\n"
        f"   {authorize_url}\n"
        "\n"
        "2. Claude redirects to platform.claude.com, which SHOWS you a code\n"
        "   (it does not come back to this machine).\n"
        "3. Copy that code — or the full address — and paste it below, then Enter.\n"
        "   Do this in THIS run: the PKCE verifier and state are per-run, so a\n"
        "   second `claude-login` invalidates the URL you just opened.\n"
    )


def perform_claude_login(
    *,
    open_browser: Callable[[str], bool] | None = None,
    read_input: Callable[[], str] = input,
    emit: Callable[[str], None] = _default_emit,
    exchanger: CodeExchanger | None = None,
    state_factory: Callable[[], str] | None = None,
    now_ms: Callable[[], int] | None = None,
) -> ClaudeLoginResult:
    """Local PKCE flow — open the browser, then read the pasted code.

    This USED to bind a loopback server and capture the callback. The platform
    no longer accepts a loopback ``redirect_uri`` (2026-09-17: the authorize page
    answers "Invalid request format" before any token call), so there is nothing
    to capture — the code is shown on ``platform.claude.com`` instead. Keeping a
    loopback path would only be a trap that fails at the same place.

    The only thing separating this from :func:`perform_claude_login_manual` is
    that it opens the browser for you.
    """
    open_fn = open_browser or webbrowser.open
    return _paste_back_login(
        announce=lambda url: (
            None if open_fn(url) else logger.warning("claude_login_browser_open_failed", url=url)
        ),
        read_input=read_input,
        emit=emit,
        exchanger=exchanger,
        state_factory=state_factory,
        now_ms=now_ms,
    )


def _paste_back_login(
    *,
    announce: Callable[[str], Any],
    read_input: Callable[[], str],
    emit: Callable[[str], None],
    exchanger: CodeExchanger | None,
    state_factory: Callable[[], str] | None,
    now_ms: Callable[[], int] | None,
) -> ClaudeLoginResult:
    """The one real flow: authorize out-of-band, paste the code back, exchange.

    ``state`` is generated here and used at the exchange (the token endpoint
    validates it), so a mangled pasted ``state`` is a warning rather than a hard
    failure — the pasted value is only a best-effort client-side CSRF pre-check.
    """
    exchange_fn = exchanger or _http_exchange_code
    # 32 bytes, not 16: MEASURED 2026-09-17 that the authorize endpoint refuses
    # the shorter one with "Invalid request format" before any token call.
    # Three URLs, identical in all eight parameters except this, opened in a
    # logged-in browser: real CLI (43 chars) OK · ours (22) REFUSED · ours (43) OK.
    state = (state_factory or (lambda: secrets.token_urlsafe(_STATE_BYTES)))()
    now = (now_ms or (lambda: int(time.time() * 1000)))()

    verifier, challenge = make_pkce_pair()
    authorize_url = make_claude_authorize_url(
        redirect_uri=CLAUDE_REDIRECT_URI, challenge=challenge, state=state
    )
    announce(authorize_url)
    emit(_manual_instructions(authorize_url))
    parsed = parse_claude_callback_input(read_input())
    code = parsed["code"]
    if code is None:  # pragma: no cover — parse_claude_callback_input guarantees a code
        raise ClaudeLoginError("no `code` in pasted callback input")
    if parsed["state"] is not None and parsed["state"] != state:
        logger.warning("claude_login_pasted_state_mismatch")
    payload = exchange_fn(
        code=code, code_verifier=verifier, redirect_uri=CLAUDE_REDIRECT_URI, state=state
    )
    return _result_from_payload(payload, now_ms=now)


def perform_claude_login_manual(
    *,
    read_input: Callable[[], str] = input,
    emit: Callable[[str], None] = _default_emit,
    exchanger: CodeExchanger | None = None,
    state_factory: Callable[[], str] | None = None,
    now_ms: Callable[[], int] | None = None,
) -> ClaudeLoginResult:
    """Headless variant — identical, minus opening a browser on this machine."""
    return _paste_back_login(
        announce=lambda _url: None,
        read_input=read_input,
        emit=emit,
        exchanger=exchanger,
        state_factory=state_factory,
        now_ms=now_ms,
    )


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
