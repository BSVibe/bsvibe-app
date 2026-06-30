"""Worker-managed Claude OAuth — refresh-on-near-expiry, persist, soft-fail.

The launchd-spawned claude can't read the Keychain → falls back to a stale
on-disk token → 401. The worker instead keeps its own credential file and
refreshes the access token before expiry, injecting it as ANTHROPIC_AUTH_TOKEN.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.executors.worker.claude_auth import ensure_claude_bearer

_NOW = 1_700_000_000_000  # fixed "now" in ms
_HOUR = 3600 * 1000


def _write(path: Path, *, access: str, refresh: str, expires_at: int) -> None:
    path.write_text(
        json.dumps({"access_token": access, "refresh_token": refresh, "expires_at": expires_at})
    )


def test_returns_token_when_not_near_expiry(tmp_path) -> None:
    p = tmp_path / "oauth.json"
    _write(p, access="live-token", refresh="r0", expires_at=_NOW + 8 * _HOUR)
    calls: list[str] = []

    def _refresher(rt: str) -> dict[str, Any]:
        calls.append(rt)
        raise AssertionError("must NOT refresh a token that is far from expiry")

    out = ensure_claude_bearer(p, now_ms=_NOW, refresher=_refresher)
    assert out == "live-token"
    assert calls == []  # no network


def test_refreshes_when_near_expiry_and_persists(tmp_path) -> None:
    p = tmp_path / "oauth.json"
    _write(p, access="stale", refresh="r0", expires_at=_NOW + 60_000)  # within 600s buffer
    seen: list[str] = []

    def _refresher(rt: str) -> dict[str, Any]:
        seen.append(rt)
        return {"access_token": "fresh", "refresh_token": "r1", "expires_in": 28800}

    out = ensure_claude_bearer(p, now_ms=_NOW, refresher=_refresher)
    assert out == "fresh"
    assert seen == ["r0"]  # refreshed using the current refresh token
    # rotated pair persisted atomically
    saved = json.loads(p.read_text())
    assert saved["access_token"] == "fresh"
    assert saved["refresh_token"] == "r1"
    assert saved["expires_at"] == _NOW + 28800 * 1000


def test_expired_token_refreshes(tmp_path) -> None:
    p = tmp_path / "oauth.json"
    _write(p, access="old", refresh="r0", expires_at=_NOW - _HOUR)  # already expired

    def _refresher(rt: str) -> dict[str, Any]:
        return {"access_token": "new", "refresh_token": "r1", "expires_in": 28800}

    assert ensure_claude_bearer(p, now_ms=_NOW, refresher=_refresher) == "new"


def test_refresh_failure_soft_falls_back(tmp_path) -> None:
    p = tmp_path / "oauth.json"
    _write(p, access="stale", refresh="r0", expires_at=_NOW - _HOUR)

    def _refresher(rt: str) -> dict[str, Any]:
        raise OSError("network down")

    # Soft-fail: returns the (stale) access token rather than crashing; the file
    # is left untouched so the refresh token is not lost.
    out = ensure_claude_bearer(p, now_ms=_NOW, refresher=_refresher)
    assert out == "stale"
    saved = json.loads(p.read_text())
    assert saved["refresh_token"] == "r0"  # not consumed/clobbered


def test_missing_file_returns_none(tmp_path) -> None:
    assert (
        ensure_claude_bearer(tmp_path / "nope.json", now_ms=_NOW, refresher=lambda _r: {}) is None
    )


def test_tolerates_claude_cli_wrapper_shape(tmp_path) -> None:
    """Seeding compat: a ``{"claudeAiOauth": {...}}`` file (the CLI's own shape)
    is read transparently."""
    p = tmp_path / "creds.json"
    p.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "live",
                    "refreshToken": "r0",
                    "expiresAt": _NOW + 8 * _HOUR,
                }
            }
        )
    )
    assert ensure_claude_bearer(p, now_ms=_NOW, refresher=lambda _r: {}) == "live"
