"""Tests for :mod:`backend.executors.worker.claude_login`.

``bsvibe-worker claude-login`` runs an authorize-code PKCE flow against the
Claude Code OAuth app (the SAME client_id/endpoints the ``claude`` CLI uses)
and persists the resulting token pair into the worker's OWN
``~/.bsvibe/claude_oauth.json`` — an INDEPENDENT refresh-token family, so the
worker never needs re-seeding from the CLI creds (which caused the mutual-burn:
shared family → CLI rotation invalidated the worker's copy).

Every side-effect (browser open, loopback callback, the token-endpoint POST,
state generation, clock) is injectable so the whole orchestration runs with no
network, no stdin/stdout, and no real files.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from backend.executors.worker.claude_login import (
    _CLAUDE_SCOPE,
    ClaudeLoginError,
    ClaudeLoginResult,
    make_claude_authorize_url,
    make_pkce_pair,
    parse_claude_callback_input,
    perform_claude_login,
    perform_claude_login_manual,
    run_claude_login,
)


# --------------------------------------------------------------------------- #
# PKCE + authorize URL
# --------------------------------------------------------------------------- #
def test_make_pkce_pair_url_safe_base64() -> None:
    verifier, challenge = make_pkce_pair()
    assert len(verifier) >= 43  # RFC 7636 §4.1
    assert len(challenge) >= 43
    assert all(c.isalnum() or c in "-_" for c in verifier)
    assert all(c.isalnum() or c in "-_" for c in challenge)


def test_authorize_url_scope_and_pkce() -> None:
    url = make_claude_authorize_url(
        redirect_uri="http://localhost:60400/callback",
        challenge="CHALLENGE123",
        state="STATE456",
    )
    assert url.startswith("https://claude.com/cai/oauth/authorize?")
    q = parse_qs(urlparse(url).query)
    # Static Claude Code OAuth client id — same one the CLI + refresh flow use.
    assert q["client_id"] == ["9d1c250a-e61b-44d9-88ed-5944d1962f5e"]
    assert q["response_type"] == ["code"]
    assert q["code"] == ["true"]
    assert q["code_challenge"] == ["CHALLENGE123"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["STATE456"]
    assert q["redirect_uri"] == ["http://localhost:60400/callback"]
    # MEASURED: the authorize endpoint rejects the full CLI scope set for a raw
    # URL; user:inference alone is accepted AND yields a refreshing token.
    assert q["scope"] == ["user:inference"]
    assert _CLAUDE_SCOPE == "user:inference"


# --------------------------------------------------------------------------- #
# parse_claude_callback_input
# --------------------------------------------------------------------------- #
def test_parse_callback_code_hash_state() -> None:
    # Claude's platform callback page shows ``<code>#<state>`` for paste-back.
    parsed = parse_claude_callback_input("ABC123#STATE789")
    assert parsed == {"code": "ABC123", "state": "STATE789"}


def test_parse_callback_full_url() -> None:
    parsed = parse_claude_callback_input("http://127.0.0.1:60396/callback?code=XYZ&state=ST")
    assert parsed == {"code": "XYZ", "state": "ST"}


def test_parse_callback_bare_code() -> None:
    parsed = parse_claude_callback_input("ONLYCODE")
    assert parsed == {"code": "ONLYCODE", "state": None}


def test_parse_callback_empty_raises() -> None:
    with pytest.raises(ClaudeLoginError):
        parse_claude_callback_input("   ")


def test_parse_callback_whitespace_bare_raises() -> None:
    with pytest.raises(ClaudeLoginError):
        parse_claude_callback_input("has space")


def test_parse_callback_url_without_code_raises() -> None:
    with pytest.raises(ClaudeLoginError, match="code"):
        parse_claude_callback_input("http://127.0.0.1:9/callback?state=only")


def test_parse_callback_hash_empty_code_raises() -> None:
    with pytest.raises(ClaudeLoginError, match="no code"):
        parse_claude_callback_input("#JUSTSTATE")


# --------------------------------------------------------------------------- #
# Manual (remote paste-back) flow
# --------------------------------------------------------------------------- #
def _fake_exchanger(recorder: dict[str, object]):
    def _exchange(
        *, code: str, code_verifier: str, redirect_uri: str, state: str
    ) -> dict[str, object]:
        recorder["code"] = code
        recorder["code_verifier"] = code_verifier
        recorder["redirect_uri"] = redirect_uri
        recorder["state"] = state
        return {
            "access_token": "sk-ant-oat01-NEWACCESS",
            "refresh_token": "sk-ant-ort01-NEWREFRESH",
            "expires_in": 3600,
        }

    return _exchange


def test_perform_manual_happy_path_returns_token() -> None:
    rec: dict[str, object] = {}
    emitted: list[str] = []

    result = perform_claude_login_manual(
        read_input=lambda: "PASTEDCODE#FIXEDSTATE",
        emit=emitted.append,
        exchanger=_fake_exchanger(rec),
        state_factory=lambda: "FIXEDSTATE",
        now_ms=lambda: 1_000_000,
    )

    assert isinstance(result, ClaudeLoginResult)
    assert result.access_token == "sk-ant-oat01-NEWACCESS"
    assert result.refresh_token == "sk-ant-ort01-NEWREFRESH"
    assert result.expires_at_ms == 1_000_000 + 3600 * 1000
    # The exchange used the pasted code, our verifier, a loopback redirect, and
    # the GENERATED state (which the token endpoint validates).
    assert rec["code"] == "PASTEDCODE"
    assert str(rec["redirect_uri"]).startswith("http://localhost:")  # type: ignore[arg-type]
    assert str(rec["redirect_uri"]).endswith("/callback")  # type: ignore[arg-type]
    assert rec["state"] == "FIXEDSTATE"
    assert isinstance(rec["code_verifier"], str) and len(rec["code_verifier"]) >= 43  # type: ignore[arg-type]
    # The authorize URL was surfaced to the operator with the measured scope.
    assert any("claude.com/cai/oauth/authorize" in m for m in emitted)
    assert any("scope=user%3Ainference" in m for m in emitted)


def test_perform_manual_mangled_pasted_state_still_succeeds() -> None:
    # MEASURED: a pasted redirect URL can carry a mangled `state` (copy artifact).
    # The exchange uses the GENERATED state (server-validated), so a pasted-state
    # mismatch is a warning, not a hard failure.
    rec: dict[str, object] = {}
    result = perform_claude_login_manual(
        read_input=lambda: "CODE#EXPECTEDSTATEclient_id%3D9d1c",
        emit=lambda _m: None,
        exchanger=_fake_exchanger(rec),
        state_factory=lambda: "EXPECTEDSTATE",
        now_ms=lambda: 0,
    )
    assert result.access_token == "sk-ant-oat01-NEWACCESS"
    assert rec["code"] == "CODE"
    assert rec["state"] == "EXPECTEDSTATE"  # generated state, not the pasted one


def test_perform_manual_bare_code_skips_state_check() -> None:
    # A bare code (no #state) is accepted — Claude sometimes shows only the code.
    rec: dict[str, object] = {}
    result = perform_claude_login_manual(
        read_input=lambda: "BARECODE",
        emit=lambda _m: None,
        exchanger=_fake_exchanger(rec),
        state_factory=lambda: "IGNORED",
        now_ms=lambda: 0,
    )
    assert result.access_token == "sk-ant-oat01-NEWACCESS"
    assert rec["code"] == "BARECODE"


def test_perform_manual_missing_refresh_token_raises() -> None:
    def _no_refresh(
        *, code: str, code_verifier: str, redirect_uri: str, state: str
    ) -> dict[str, object]:  # noqa: ARG001
        return {"access_token": "A", "expires_in": 3600}  # no refresh_token

    with pytest.raises(ClaudeLoginError, match="refresh_token"):
        perform_claude_login_manual(
            read_input=lambda: "C#S",
            emit=lambda _m: None,
            exchanger=_no_refresh,
            state_factory=lambda: "S",
        )


# --------------------------------------------------------------------------- #
# Loopback flow
# --------------------------------------------------------------------------- #
def test_perform_loopback_happy_path() -> None:
    rec: dict[str, object] = {}
    captured_state: dict[str, str] = {}

    def _open_browser(url: str) -> bool:
        captured_state["state"] = parse_qs(urlparse(url).query)["state"][0]
        return True

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "LOOPCODE", "state": captured_state["state"]}

    result = perform_claude_login(
        open_browser=_open_browser,
        wait_for_callback=_wait_for_callback,
        exchanger=_fake_exchanger(rec),
        pick_port=lambda: 60400,
        now_ms=lambda: 0,
    )
    assert result.access_token == "sk-ant-oat01-NEWACCESS"
    assert rec["code"] == "LOOPCODE"
    assert rec["state"] == captured_state["state"]
    # Loopback redirect must be the exact localhost:port the callback listened on.
    assert rec["redirect_uri"] == "http://localhost:60400/callback"


def test_perform_loopback_state_mismatch_raises() -> None:
    def _open_browser(url: str) -> bool:  # noqa: ARG001
        return True

    def _wait_for_callback(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "C", "state": "ATTACKER"}

    with pytest.raises(ClaudeLoginError, match="state"):
        perform_claude_login(
            open_browser=_open_browser,
            wait_for_callback=_wait_for_callback,
            exchanger=_fake_exchanger({}),
            pick_port=lambda: 60400,
        )


# --------------------------------------------------------------------------- #
# run_claude_login — persists flat {access_token, refresh_token, expires_at}
# --------------------------------------------------------------------------- #
def test_run_claude_login_persists_flat_0600(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "claude_oauth.json"

    result = run_claude_login(
        manual=True,
        path=target,
        read_input=lambda: "C#S",
        emit=lambda _m: None,
        exchanger=_fake_exchanger({}),
        state_factory=lambda: "S",
        now_ms=lambda: 2_000_000,
    )

    assert target.exists()
    data = json.loads(target.read_text())
    # Exactly the flat shape _persist / _read_oauth expect.
    assert data == {
        "access_token": "sk-ant-oat01-NEWACCESS",
        "refresh_token": "sk-ant-ort01-NEWREFRESH",
        "expires_at": 2_000_000 + 3600 * 1000,
    }
    assert (target.stat().st_mode & 0o777) == 0o600
    assert result.access_token == "sk-ant-oat01-NEWACCESS"


def test_run_claude_login_loopback_branch(tmp_path: Path) -> None:
    target = tmp_path / "claude_oauth.json"
    captured_state: dict[str, str] = {}
    rec: dict[str, object] = {}

    def _open_browser(url: str) -> bool:
        captured_state["state"] = parse_qs(urlparse(url).query)["state"][0]
        return True

    def _wait(port: int, timeout: float) -> dict[str, str]:  # noqa: ARG001
        return {"code": "LC", "state": captured_state["state"]}

    run_claude_login(
        manual=False,
        path=target,
        open_browser=_open_browser,
        wait_for_callback=_wait,
        exchanger=_fake_exchanger(rec),
        pick_port=lambda: 60401,
        now_ms=lambda: 0,
    )
    assert json.loads(target.read_text())["access_token"] == "sk-ant-oat01-NEWACCESS"


# --------------------------------------------------------------------------- #
# Default HTTP side-effects (token exchange, port pick, loopback callback)
# --------------------------------------------------------------------------- #
def test_http_exchange_code_posts_authorization_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request as _ur

    from backend.executors.worker import claude_login as mod

    seen: dict[str, object] = {}

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"access_token": "A", "refresh_token": "R", "expires_in": 10}
            ).encode()

    def _fake_urlopen(req: object, timeout: float = 0):  # noqa: ANN202, ARG001
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        seen["body"] = json.loads(req.data.decode())  # type: ignore[attr-defined]
        seen["ua"] = req.headers.get("User-agent")  # type: ignore[attr-defined]
        return _Resp()

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)

    payload = mod._http_exchange_code(
        code="CODE", code_verifier="VER", redirect_uri="https://rd/cb", state="ST8"
    )
    assert payload["access_token"] == "A"
    assert seen["url"] == "https://platform.claude.com/v1/oauth/token"
    body = seen["body"]
    assert body["grant_type"] == "authorization_code"  # type: ignore[index]
    assert body["code"] == "CODE"  # type: ignore[index]
    assert body["code_verifier"] == "VER"  # type: ignore[index]
    # MEASURED: the token endpoint requires `state` in the authorization_code body.
    assert body["state"] == "ST8"  # type: ignore[index]
    assert "claude-cli" in str(seen["ua"])  # Cloudflare-passing UA


def test_http_exchange_code_network_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request as _ur

    from backend.executors.worker import claude_login as mod

    def _boom(req: object, timeout: float = 0):  # noqa: ANN202, ARG001
        raise OSError("no network")

    monkeypatch.setattr(_ur, "urlopen", _boom)
    with pytest.raises(ClaudeLoginError, match="token exchange failed"):
        mod._http_exchange_code(code="C", code_verifier="V", redirect_uri="r", state="s")


def test_pick_loopback_port_returns_free_port() -> None:
    from backend.executors.worker import claude_login as mod

    port = mod._pick_loopback_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536


def test_wait_for_callback_captures_code() -> None:
    import threading
    import time as _time
    import urllib.request as _ur

    from backend.executors.worker import claude_login as mod

    port = mod._pick_loopback_port()

    def _fire() -> None:
        # Poll until the one-shot server is up, then hit the callback.
        for _ in range(50):
            try:
                _ur.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{port}/callback?code=CBCODE&state=CBSTATE", timeout=1
                ).read()
                return
            except OSError:
                _time.sleep(0.05)

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    captured = mod._wait_for_callback(port, timeout=5.0)
    t.join(timeout=2.0)
    assert captured["code"] == "CBCODE"
    assert captured["state"] == "CBSTATE"


def test_wait_for_callback_timeout_raises() -> None:
    from backend.executors.worker import claude_login as mod

    port = mod._pick_loopback_port()
    with pytest.raises(ClaudeLoginError, match="timed out"):
        mod._wait_for_callback(port, timeout=0.3)
