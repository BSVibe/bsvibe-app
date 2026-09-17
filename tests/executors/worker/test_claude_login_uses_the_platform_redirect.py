"""The authorize request must use the redirect the CLI actually uses today.

2026-09-17: `bsvibe-worker claude-login` never reached the token step — the
authorize PAGE rejected it with **"Invalid request format"**, the same string
this module's docstring records for the *wrong* shape back on 2026-07-31.

Measured against the real CLI (v2.1.268, `claude setup-token`, captured over a
pty). Every parameter matches ours except one:

    redirect_uri = https://platform.claude.com/oauth/code/callback

The module comment says the opposite — *"loopback MEASURED to be accepted; the
platform out-of-band redirect is not"*. That was true in July and is false now;
the platform flipped which redirect it accepts. A measurement has a date, and
this one expired.

Out-of-band is also the better fit for the case that forced this: a worker
reached over SSH has no browser and nothing can listen on its localhost, which
is exactly what the loopback flow required.
"""

from __future__ import annotations

import urllib.parse

from backend.executors.worker import claude_login

#: Captured from `claude setup-token` (v2.1.268) on 2026-09-17.
REAL_CLI_REDIRECT = "https://platform.claude.com/oauth/code/callback"


def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_authorize_url_uses_the_platform_callback() -> None:
    url = claude_login.make_claude_authorize_url(
        redirect_uri=claude_login.CLAUDE_REDIRECT_URI, challenge="CH", state="ST"
    )
    assert _params(url)["redirect_uri"] == REAL_CLI_REDIRECT


def test_the_default_redirect_is_the_platform_callback() -> None:
    """The constant itself is the pin — a caller should not have to know it."""
    assert claude_login.CLAUDE_REDIRECT_URI == REAL_CLI_REDIRECT


def test_authorize_url_still_matches_the_real_cli_on_every_other_param() -> None:
    """The one thing that changed is the redirect; pin the rest so a future
    drift is attributed precisely instead of re-debugged from scratch."""
    url = claude_login.make_claude_authorize_url(
        redirect_uri=claude_login.CLAUDE_REDIRECT_URI, challenge="CH", state="ST"
    )
    got = _params(url)
    assert got["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    assert got["scope"] == "user:inference"
    assert got["response_type"] == "code"
    assert got["code"] == "true"
    assert got["code_challenge_method"] == "S256"


def test_the_manual_flow_exchanges_with_the_same_redirect_it_authorized_with() -> None:
    """The token endpoint validates redirect_uri — authorizing with one and
    exchanging with another is a 400 that says nothing useful."""
    seen: dict[str, str] = {}
    emitted: list[str] = []

    def _exchanger(*, code: str, code_verifier: str, redirect_uri: str, state: str) -> dict:
        seen["redirect_uri"] = redirect_uri
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    claude_login.perform_claude_login_manual(
        read_input=lambda: "code123#ST",
        emit=emitted.append,
        exchanger=_exchanger,
        state_factory=lambda: "ST",
        now_ms=lambda: 0,
    )
    assert seen["redirect_uri"] == REAL_CLI_REDIRECT
    # And the operator is pointed at the platform page, not a dead localhost.
    assert REAL_CLI_REDIRECT in emitted[0] or "platform.claude.com" in emitted[0]
