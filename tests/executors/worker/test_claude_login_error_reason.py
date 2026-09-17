"""A 400 from the token endpoint must say WHY (#965 follow-up).

`bsvibe-worker claude-login` failed for the founder with nothing but::

    token exchange failed: HTTP Error 400: Bad Request

That is `str(urllib.error.HTTPError)`, which **drops the response body** — and the
body is the only place the reason lives. OAuth spells its failures there:
``invalid_grant`` (the code was already used / expired), ``invalid_request``
(a malformed or mismatched field), ``unauthorized_client``. Those three call for
three different actions, and the operator could not tell them apart.
"""

from __future__ import annotations

import io
import urllib.error

import pytest


def _raise_400(body: bytes):
    def _fake_urlopen(*_a, **_k):
        raise urllib.error.HTTPError(
            url="https://platform.claude.com/v1/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

    return _fake_urlopen


def test_the_400_body_reaches_the_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.executors.worker import claude_login

    monkeypatch.setattr(
        claude_login.urllib.request,
        "urlopen",
        _raise_400(b'{"error":"invalid_grant","error_description":"code expired"}'),
    )
    with pytest.raises(claude_login.ClaudeLoginError) as caught:
        claude_login._http_exchange_code(
            code="c", code_verifier="v", redirect_uri="http://localhost:1/callback", state="s"
        )
    message = str(caught.value)
    assert "invalid_grant" in message, message
    assert "code expired" in message, message


def test_a_bodyless_failure_still_names_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degrading to the old message is fine — going SILENT is not."""
    from backend.executors.worker import claude_login

    monkeypatch.setattr(claude_login.urllib.request, "urlopen", _raise_400(b""))
    with pytest.raises(claude_login.ClaudeLoginError) as caught:
        claude_login._http_exchange_code(
            code="c", code_verifier="v", redirect_uri="http://localhost:1/callback", state="s"
        )
    assert "400" in str(caught.value)
