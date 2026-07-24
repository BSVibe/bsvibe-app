"""Worker-managed Claude OAuth — keep the ``claude_code`` executor authenticated
without depending on the OS Keychain.

Why this exists: a launchd/systemd-spawned ``claude`` CLI cannot read the macOS
Keychain (its security session can't unlock the login keychain), so it silently
falls back to a stale ``~/.claude/.credentials.json`` token → ``401 Failed to
authenticate`` — even though the same command works in an interactive shell
([[launchd-daemon-cli-keychain-auth-fallback]]). Rather than pin a static token
in the launchd plist (expires in hours), the worker owns the OAuth lifecycle:
it keeps its OWN credential file, refreshes the access token when it nears
expiry, and the executor injects it as ``ANTHROPIC_AUTH_TOKEN`` per invocation
(an env var the subprocess sanitizer preserves, unlike ``CLAUDE_CODE_*``, and
which overrides the keychain).

Design notes:
* The credential file is the worker's OWN (default ``~/.bsvibe/claude_oauth.json``),
  separate from ``~/.claude/.credentials.json`` so an interactive ``claude
  /login`` (which rewrites the latter) never clobbers the worker's token family.
* OAuth refresh tokens are SINGLE-USE — a refresh rotates the token, so the new
  pair MUST be persisted atomically. An ``flock`` serialises the whole
  read→refresh→write so two worker processes never both consume the same refresh
  token (the loser would get ``invalid_grant``).
* Everything is soft-fail: any error returns ``None`` and the executor falls
  back to the CLI's own auth (keychain/file) — never crashes a task.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from backend.executors.worker.config import get_worker_settings

logger = structlog.get_logger(__name__)

# Public Claude Code OAuth client id + token endpoint (same as the CLI uses).
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 — URL, not a secret
# Refresh this many seconds BEFORE the access token's expiry.
_REFRESH_BUFFER_S = 600
_HTTP_TIMEOUT_S = 30

#: An OAuth refresher: takes the current refresh token, returns the provider's
#: token response (``access_token`` / ``refresh_token`` / ``expires_in``).
Refresher = Callable[[str], dict[str, Any]]


def default_oauth_path() -> Path:
    """The worker's own credential file (``BSVIBE_WORKER_CLAUDE_OAUTH_PATH`` via
    :class:`WorkerSettings`, else ``~/.bsvibe/claude_oauth.json``)."""
    configured = get_worker_settings().claude_oauth_path
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".bsvibe" / "claude_oauth.json"


def default_cli_credentials_path() -> Path:
    """The interactive ``claude`` CLI's own credential file
    (``BSVIBE_WORKER_CLAUDE_CLI_CREDENTIALS_PATH`` via :class:`WorkerSettings`,
    else ``~/.claude/.credentials.json``).

    This file is owned/refreshed by the CLI itself; the worker only ever READS
    it, as a last-resort fallback, and borrows its access token — it never adopts
    the CLI's single-use refresh token (that would let the worker's next refresh
    rotate and burn the CLI's own login — the exact mutual-burn the separate-file
    design avoids)."""
    configured = get_worker_settings().claude_cli_credentials_path
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / ".credentials.json"


def _read_oauth(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    # Tolerate the claude CLI's ``{"claudeAiOauth": {...}}`` shape (seeding compat).
    if isinstance(data, dict) and isinstance(data.get("claudeAiOauth"), dict):
        data = data["claudeAiOauth"]
    return data if isinstance(data, dict) else None


def _expires_at_ms(oauth: dict[str, Any]) -> int:
    # Support both the flat ``expires_at`` (ms) and the CLI's ``expiresAt`` (ms).
    raw = oauth.get("expires_at", oauth.get("expiresAt", 0))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _access_token(oauth: dict[str, Any]) -> str:
    return str(oauth.get("access_token") or oauth.get("accessToken") or "")


def _refresh_token(oauth: dict[str, Any]) -> str:
    return str(oauth.get("refresh_token") or oauth.get("refreshToken") or "")


def _http_refresh(refresh_token: str) -> dict[str, Any]:
    """Default refresher — POST the OAuth token endpoint. UA mimics the CLI so
    Cloudflare's bot filter (error 1010) lets the request through."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed https OAuth endpoint
        _TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-cli/2.1.172 (external, cli)",
        },
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode())
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("oauth refresh response missing access_token")
    return payload


def _persist(path: Path, access: str, refresh: str, expires_at_ms: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"access_token": access, "refresh_token": refresh, "expires_at": expires_at_ms},
            indent=2,
        )
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _cli_fallback_bearer(cli_path: Path, *, now_ms: int) -> str | None:
    """Last-resort, READ-ONLY borrow of the interactive CLI's live access token.

    Reads ``cli_path`` via :func:`_read_oauth` (which unwraps the CLI's
    ``{"claudeAiOauth": {...}}`` shape) and returns its access token ONLY IF that
    token is present and NOT within :data:`_REFRESH_BUFFER_S` of expiry (i.e.
    currently valid). Otherwise ``None``.

    Deliberately returns the ACCESS token only — the caller must never persist it
    into the worker file nor adopt the CLI's single-use refresh token, or the
    worker's next refresh would rotate and BURN the CLI's own login.
    """
    oauth = _read_oauth(cli_path)
    if oauth is None:
        return None
    access = _access_token(oauth)
    if not access:
        return None
    if now_ms < _expires_at_ms(oauth) - _REFRESH_BUFFER_S * 1000:
        return access
    return None


def _resolve_fallback(cli_path: Path, *, now_ms: int, stale: str | None) -> str | None:
    """Try the CLI fallback; on success return it (borrowed live access token),
    else return ``stale`` (the pre-existing behaviour: stale worker token or
    ``None``). Emits a distinct log for each outcome so a future outage is
    diagnosable in one grep."""
    borrowed = _cli_fallback_bearer(cli_path, now_ms=now_ms)
    if borrowed is not None:
        logger.info("claude_oauth_cli_fallback_used")
        return borrowed
    logger.info("claude_oauth_cli_fallback_unavailable")
    return stale


def _is_invalid_grant(exc: BaseException) -> bool:
    """Best-effort detection of an OAuth ``invalid_grant`` (burned single-use
    refresh token) — an HTTP 400 whose body mentions ``invalid_grant``."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 400:
        return False
    try:
        return "invalid_grant" in exc.read().decode(errors="replace")
    except Exception:  # noqa: BLE001 — diagnostics only, never raise
        return False


def ensure_claude_bearer(
    path: Path | None = None,
    *,
    now_ms: int | None = None,
    refresher: Refresher | None = None,
    cli_path: Path | None = None,
) -> str | None:
    """Return a currently-valid Claude OAuth access token, refreshing if needed.

    Reads the worker credential file; if the access token is missing or within
    :data:`_REFRESH_BUFFER_S` of expiry, refreshes via ``refresher`` (default
    :func:`_http_refresh`) under an ``flock`` and persists the rotated pair.

    On ANY failure of that primary path — missing/malformed worker file, expiry
    with no refresh token, or a failed refresh (e.g. a burned single-use refresh
    token returning ``invalid_grant``) — it falls back, as a LAST RESORT, to the
    interactive CLI's fresh credential (``cli_path``, default
    :func:`default_cli_credentials_path`) via :func:`_cli_fallback_bearer`. That
    fallback is strictly READ-ONLY: it borrows the CLI's live access token per
    call and NEVER writes the worker file nor adopts the CLI's single-use refresh
    token (which would mutually-burn the CLI's own login). This self-heals a
    burned worker refresh token instead of silently downing the executor.

    Everything is soft-fail — any error returns a token or ``None``, never raises.
    Safe to call per invocation; it only hits the network when actually near
    expiry.
    """
    path = path or default_oauth_path()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    refresher = refresher or _http_refresh
    cli_path = cli_path or default_cli_credentials_path()

    oauth = _read_oauth(path)
    if oauth is None:
        # Worker file missing/malformed — borrow the CLI's live token if any.
        return _resolve_fallback(cli_path, now_ms=now, stale=None)
    access = _access_token(oauth)
    if access and now < _expires_at_ms(oauth) - _REFRESH_BUFFER_S * 1000:
        return access  # still valid — no refresh, no network, CLI not read.

    refresh = _refresh_token(oauth)
    if not refresh:
        # Expired/near-expiry with no refresh token — cannot self-refresh.
        return _resolve_fallback(cli_path, now_ms=now, stale=access or None)

    # Serialise the whole refresh across processes: single-use refresh tokens mean
    # two concurrent refreshers would have one fail with invalid_grant.
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Double-check: another process may have refreshed while we waited.
            oauth = _read_oauth(path) or oauth
            access = _access_token(oauth)
            if access and now < _expires_at_ms(oauth) - _REFRESH_BUFFER_S * 1000:
                return access
            refresh = _refresh_token(oauth) or refresh
            payload = refresher(refresh)
            new_access = str(payload["access_token"])
            new_refresh = str(payload.get("refresh_token") or refresh)
            expires_in = int(payload.get("expires_in") or 0)
            new_expires_at = now + expires_in * 1000 if expires_in else _expires_at_ms(oauth)
            _persist(path, new_access, new_refresh, new_expires_at)
            logger.info("claude_oauth_refreshed", expires_in=expires_in)
            return new_access
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        if _is_invalid_grant(exc):
            # The refresh token was burned (rotated server-side, our copy stale).
            logger.warning("claude_oauth_refresh_invalid_grant")
        logger.warning("claude_oauth_refresh_failed", exc_info=True)
        # Worker file untouched (refresh token not consumed); borrow the CLI's
        # live token as a last resort before returning the stale worker access.
        return _resolve_fallback(cli_path, now_ms=now, stale=access or None)


__all__ = ["default_cli_credentials_path", "default_oauth_path", "ensure_claude_bearer"]
