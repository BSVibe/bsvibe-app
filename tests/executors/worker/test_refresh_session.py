"""``bsvibe refresh`` — redeem the stored refresh token instead of re-authing.

#871 split the expired session into two states because one of them said the
session *could* be renewed. Nothing redeemed it, so that state's advice was
identical to the other's: run `bsvibe login`. This closes it.

The load-bearing constraint, read out of the server: the token endpoint takes
``client_id`` as a REQUIRED form field, and ``rotate_refresh_token`` rejects the
grant when ``parent.client_id != client_id``. Login registers an ANONYMOUS DCR
client per sign-in (``login.py`` — "we never want a static client_id") and never
persisted the id it got. So a refresh is impossible for any credential written
before this change, and the status advice must say so rather than name a command
that will fail for the reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.executors.worker.cli import SessionState, evaluate_session
from backend.executors.worker.credentials import (
    HostCredentials,
    load_host_credentials,
    save_host_credentials,
)
from backend.executors.worker.login import LoginError, refresh_session

_ISSUER = "https://api.bsvibe.example"


def _expired(**over: object) -> HostCredentials:
    base: dict[str, object] = {
        "access_token": "DEAD-ACCESS",
        "refresh_token": "STORED-REFRESH",
        "expires_at": 1_000_000,
        "issuer": _ISSUER,
        "client_id": "dcr-from-login",
    }
    base.update(over)
    return HostCredentials(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The credential must carry the client_id, or nothing downstream can work
# ---------------------------------------------------------------------------


def test_credentials_round_trip_the_client_id(tmp_path: Path) -> None:
    """``client_id`` survives a save/load cycle — without it the refresh grant
    cannot be formed at all (the server requires the field and matches it)."""
    path = tmp_path / "credentials.json"
    save_host_credentials(_expired(), path)

    assert load_host_credentials(path).client_id == "dcr-from-login"


def test_credentials_written_before_this_change_still_load(tmp_path: Path) -> None:
    """A credentials file from an older `bsvibe login` has no ``client_id`` key.
    It must load as ``None``, never raise — the founder's file on disk right now
    is exactly this shape."""
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"access_token": "A", "refresh_token": "R", "issuer": _ISSUER}),
        encoding="utf-8",
    )

    assert load_host_credentials(path).client_id is None


def test_login_persists_the_client_id_it_registered() -> None:
    """The DCR client_id login obtained must reach the saved credential.
    If it does not, every future refresh fails with ``invalid_grant`` and the
    whole command is dead on arrival."""
    from urllib.parse import parse_qs, urlparse

    from backend.executors.worker.login import perform_login

    seen: dict[str, str] = {}

    def _open(url: str) -> bool:
        seen["state"] = parse_qs(urlparse(url).query)["state"][0]
        return True

    client = _fake_login_client()
    try:
        result = perform_login(
            issuer=_ISSUER,
            httpx_client=client,
            open_browser=_open,
            pick_port=lambda: 41234,
            wait_for_callback=lambda _p, _t: {"code": "CODE", "state": seen["state"]},
            timeout_s=5.0,
        )
    finally:
        client.close()

    assert result.credentials.client_id == "dcr-fake-123"


# ---------------------------------------------------------------------------
# The status advice must not name a command that cannot work for this reader
# ---------------------------------------------------------------------------


def test_status_offers_refresh_when_it_can_actually_work() -> None:
    """Expired + a refresh token + the client_id that minted it → the session
    really is renewable, and the advice says how."""
    status = evaluate_session(_expired(), now=2_000_000)

    assert status.state is SessionState.EXPIRED_REFRESHABLE
    joined = " ".join(status.lines)
    assert "Run `bsvibe refresh`" in joined
    assert "no bsvibe command redeems it yet" not in joined


def test_status_does_not_offer_refresh_to_a_credential_that_predates_it() -> None:
    """THE CONTROL. Without it, an implementation that always names
    ``bsvibe refresh`` passes the test above — and then tells every founder
    holding an older credential to run a command that fails for them.

    The state is still EXPIRED_REFRESHABLE (a refresh token IS stored); what
    changes is that this host cannot form the grant.

    The proposition is about what the reader is TOLD TO RUN, not about whether
    the words appear: naming `bsvibe refresh` to explain why it does not apply
    here is fine and useful. Asserting bare absence would have failed on an
    honest message and pushed the fix toward saying less."""
    status = evaluate_session(_expired(client_id=None), now=2_000_000)

    joined = " ".join(status.lines)
    assert "Run `bsvibe refresh`" not in joined
    assert "Run `bsvibe login`" in joined


# ---------------------------------------------------------------------------
# The exchange itself
# ---------------------------------------------------------------------------


def _token_client(*, status_code: int = 200) -> httpx.Client:
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/oauth/token":
            return httpx.Response(404)
        seen["form"] = dict(httpx.QueryParams(request.content.decode("utf-8")))
        if status_code >= 400:
            return httpx.Response(status_code, text="invalid_grant")
        return httpx.Response(
            200,
            json={
                "access_token": "FRESH-ACCESS",
                "refresh_token": "FRESH-REFRESH",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    client._seen = seen  # type: ignore[attr-defined]
    return client


def test_refresh_session_redeems_the_stored_token() -> None:
    """The grant is formed from what the credential carries, and the ROTATED
    pair comes back — a new access token AND a new refresh token (the server
    rotates single-use)."""
    client = _token_client()

    fresh = refresh_session(_expired(), httpx_client=client)

    form = client._seen["form"]  # type: ignore[attr-defined]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "STORED-REFRESH"
    assert form["client_id"] == "dcr-from-login"
    assert fresh.access_token == "FRESH-ACCESS"
    assert fresh.refresh_token == "FRESH-REFRESH"
    assert fresh.issuer == _ISSUER
    assert fresh.client_id == "dcr-from-login"
    assert fresh.expires_at is not None


def test_refresh_session_refuses_a_credential_it_cannot_form_a_grant_from() -> None:
    """No refresh token, or no client_id → fail BEFORE any network call, with a
    reason. Posting a grant we know is malformed only earns an opaque 400."""
    for creds in (_expired(refresh_token=None), _expired(client_id=None)):
        with pytest.raises(LoginError):
            refresh_session(creds, httpx_client=_token_client())


def test_a_rejected_refresh_does_not_destroy_the_stored_credential(
    tmp_path: Path,
) -> None:
    """The issuer can refuse (rotated already, expired, revoked). The file on
    disk must survive that untouched: overwriting it on failure would turn a
    recoverable state into a lost one."""
    path = tmp_path / "credentials.json"
    save_host_credentials(_expired(), path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(LoginError):
        refresh_session(_expired(), httpx_client=_token_client(status_code=401))

    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# helpers for the login-persists-client_id test
# ---------------------------------------------------------------------------


def _fake_login_client() -> httpx.Client:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth/register":
            return httpx.Response(201, json={"client_id": "dcr-fake-123"})
        if request.url.path == "/api/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ACCESS-LIVE",
                    "refresh_token": "REFRESH-LIVE",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(_handler))
