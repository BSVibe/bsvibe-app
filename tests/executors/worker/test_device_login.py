"""``bsvibe login --device`` — RFC 8628 polling from a host with no browser.

This is the sign-in for a machine that can reach neither a loopback listener
nor a paste-back prompt: a remote tunnel, a chat-driven session, a headless
box. The human approves the short code somewhere else entirely and the CLI
picks the credential up by polling — nothing is ever pasted back.

Correct polling is the whole feature, so that is what these pin:

* the code and URL are actually SHOWN, or the human has nothing to act on;
* ``authorization_pending`` keeps waiting, ``slow_down`` widens the interval,
  and ``access_denied`` / ``expired_token`` stop with a real reason instead of
  spinning until the operator gives up;
* a credential that arrives is persisted, because a login that does not
  survive the process is not a login.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker import login as login_mod

DEVICE_START = {
    "device_code": "dev-code-xyz",
    "user_code": "WXYZ-2345",
    "verification_uri": "https://app.bsvibe.dev/device",
    "verification_uri_complete": "https://app.bsvibe.dev/device?user_code=WXYZ-2345",
    "expires_in": 600,
    "interval": 5,
}

TOKENS = {
    "access_token": "at-123",
    "refresh_token": "rt-123",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "mcp:read mcp:write mcp:admin",
}


def _responder(token_sequence: list[httpx.Response]):
    """Serve the device-authorization POST, then the /token polls in order."""
    remaining = list(token_sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device_authorization"):
            return httpx.Response(200, json=DEVICE_START)
        if request.url.path.endswith("/token"):
            return remaining.pop(0) if remaining else httpx.Response(200, json=TOKENS)
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


def _err(code: str) -> httpx.Response:
    return httpx.Response(400, json={"error": code})


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the waits instead of taking them — the interval is the contract."""
    slept: list[float] = []
    monkeypatch.setattr(login_mod, "_device_sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def _save(creds: Any, path: Any = None) -> Any:
        box["creds"] = creds
        return "/tmp/credentials.json"

    monkeypatch.setattr(login_mod, "save_host_credentials", _save)
    return box


def _run(monkeypatch: pytest.MonkeyPatch, handler: Any) -> int:
    monkeypatch.setattr(
        cli_mod, "_device_transport", lambda: httpx.MockTransport(handler), raising=False
    )
    monkeypatch.setattr(
        login_mod, "_device_transport", lambda: httpx.MockTransport(handler), raising=False
    )
    return cli_mod.run_bsvibe_cli(["login", "--device"])


def test_parser_exposes_the_device_flag() -> None:
    # A subparser flag never shows in the TOP-LEVEL help; parse it instead.
    args = cli_mod.build_bsvibe_parser().parse_args(["login", "--device"])
    assert args.device is True
    assert cli_mod.build_bsvibe_parser().parse_args(["login"]).device is False


def test_shows_the_code_and_the_url_to_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_sleep: list[float],
    saved: dict[str, Any],
) -> None:
    rc = _run(monkeypatch, _responder([httpx.Response(200, json=TOKENS)]))
    assert rc == 0

    out = capsys.readouterr()
    combined = out.out + out.err
    # Without both of these the human has nothing to act on.
    assert DEVICE_START["user_code"] in combined
    assert DEVICE_START["verification_uri"] in combined


def test_waits_through_authorization_pending_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], saved: dict[str, Any]
) -> None:
    rc = _run(
        monkeypatch,
        _responder(
            [
                _err("authorization_pending"),
                _err("authorization_pending"),
                httpx.Response(200, json=TOKENS),
            ]
        ),
    )
    assert rc == 0
    assert saved["creds"].access_token == "at-123"
    assert saved["creds"].refresh_token == "rt-123"
    # Two waits, both at the advertised interval.
    assert no_sleep == [5, 5]


def test_slow_down_widens_the_interval(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], saved: dict[str, Any]
) -> None:
    """RFC 8628 §3.5 — the server asking us to back off must actually back off."""
    rc = _run(
        monkeypatch,
        _responder(
            [_err("slow_down"), _err("authorization_pending"), httpx.Response(200, json=TOKENS)]
        ),
    )
    assert rc == 0
    assert no_sleep[0] > 5, f"interval never widened: {no_sleep}"


def test_access_denied_stops_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_sleep: list[float],
) -> None:
    rc = _run(monkeypatch, _responder([_err("access_denied")]))
    assert rc == 1
    assert "denied" in capsys.readouterr().err.lower()


def test_expired_token_stops_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_sleep: list[float],
) -> None:
    rc = _run(monkeypatch, _responder([_err("expired_token")]))
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "expired" in err
    # It must say how to recover, not just that it failed.
    assert "again" in err or "다시" in err


def test_unexpected_error_stops_rather_than_looping_forever(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_sleep: list[float],
) -> None:
    rc = _run(monkeypatch, _responder([_err("invalid_grant")]))
    assert rc == 1
    assert "invalid_grant" in capsys.readouterr().err
