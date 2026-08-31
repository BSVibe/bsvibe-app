"""``bsvibe pat`` — mint / list / revoke a PAT without a browser on this host.

The point of these commands is the loop they close. `bsvibe login --manual`
already signs in from a remote/headless host (print URL, paste redirect back);
what was missing was a way to turn that session into the durable credential an
MCP client needs. So the behaviour worth pinning is:

* the raw token reaches stdout exactly once, and nothing else does — so
  ``TOKEN=$(bsvibe pat create --name x --quiet)`` is safe in a script;
* not being signed in is a clear instruction, not a stack trace;
* an HTTP failure is reported with the server's own words rather than swallowed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.executors.worker import cli as cli_mod
from backend.executors.worker.credentials import CredentialsNotFound, HostCredentials

CREDS = HostCredentials(
    access_token="access-token-xyz",
    refresh_token="refresh-token-xyz",
    expires_at=99999999999,
    issuer="https://api.bsvibe.dev",
)

PAT_BODY = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "mac-mini",
    "scope": ["mcp:read", "mcp:write"],
    "issued_at": "2026-08-09T00:00:00Z",
    "expires_at": None,
    "token": "eyJhbGciOiJFUzI1NiJ9.body.sig",
}


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "load_host_credentials", lambda: CREDS)


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_parser_lists_pat_subcommand() -> None:
    assert "pat" in cli_mod.build_bsvibe_parser().format_help()


def test_create_prints_only_the_token_in_quiet_mode(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json=PAT_BODY)

    monkeypatch.setattr(cli_mod, "_api_transport", lambda: _transport(handler))

    rc = cli_mod.run_bsvibe_cli(["pat", "create", "--name", "mac-mini", "--quiet"])
    assert rc == 0

    out = capsys.readouterr().out
    # Exactly the token — a script does TOKEN=$(bsvibe pat create … --quiet).
    assert out.strip() == PAT_BODY["token"]
    assert seen["url"].endswith("/api/v1/oauth/pats")
    assert seen["auth"] == f"Bearer {CREDS.access_token}"


def test_create_verbose_shows_the_token_and_says_it_is_the_only_time(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_mod, "_api_transport", lambda: _transport(lambda r: httpx.Response(201, json=PAT_BODY))
    )

    assert cli_mod.run_bsvibe_cli(["pat", "create", "--name", "mac-mini"]) == 0
    captured = capsys.readouterr()
    assert PAT_BODY["token"] in captured.out
    assert "only time" in (captured.out + captured.err).lower()


def test_create_sends_scope_and_expiry_when_given(
    signed_in: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=PAT_BODY)

    monkeypatch.setattr(cli_mod, "_api_transport", lambda: _transport(handler))

    cli_mod.run_bsvibe_cli(
        ["pat", "create", "--name", "ci", "--scope", "mcp:read", "--expires-in-days", "30"]
    )
    assert seen["body"]["name"] == "ci"
    assert seen["body"]["scope"] == ["mcp:read"]
    assert seen["body"]["expires_in_days"] == 30


def test_create_without_expiry_omits_the_field(
    signed_in: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted, not null — the server's default is 'never expires'."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=PAT_BODY)

    monkeypatch.setattr(cli_mod, "_api_transport", lambda: _transport(handler))
    cli_mod.run_bsvibe_cli(["pat", "create", "--name", "forever"])
    assert "expires_in_days" not in seen["body"]


def test_list_renders_rows(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [{k: v for k, v in PAT_BODY.items() if k != "token"}]
    monkeypatch.setattr(
        cli_mod, "_api_transport", lambda: _transport(lambda r: httpx.Response(200, json=rows))
    )

    assert cli_mod.run_bsvibe_cli(["pat", "list"]) == 0
    out = capsys.readouterr().out
    assert "mac-mini" in out
    assert PAT_BODY["id"] in out
    # A listing must never be able to show a token value.
    assert PAT_BODY["token"] not in out


def test_list_empty_says_so(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_mod, "_api_transport", lambda: _transport(lambda r: httpx.Response(200, json=[]))
    )
    assert cli_mod.run_bsvibe_cli(["pat", "list"]) == 0
    assert "no personal access tokens" in capsys.readouterr().out.lower()


def test_revoke_calls_delete(signed_in: None, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(204)

    monkeypatch.setattr(cli_mod, "_api_transport", lambda: _transport(handler))

    assert cli_mod.run_bsvibe_cli(["pat", "revoke", PAT_BODY["id"]]) == 0
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith(f"/api/v1/oauth/pats/{PAT_BODY['id']}")


def test_not_signed_in_is_an_instruction_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise() -> HostCredentials:
        raise CredentialsNotFound("no credentials file")

    monkeypatch.setattr(cli_mod, "load_host_credentials", _raise)

    assert cli_mod.run_bsvibe_cli(["pat", "create", "--name", "x"]) == 1
    err = capsys.readouterr().err
    assert "bsvibe login" in err
    # The whole reason this command exists is the browserless host.
    assert "--manual" in err


def test_http_error_surfaces_the_servers_own_words(
    signed_in: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli_mod,
        "_api_transport",
        lambda: _transport(
            lambda r: httpx.Response(
                403, json={"detail": "managing personal access tokens requires the mcp:admin scope"}
            )
        ),
    )

    assert cli_mod.run_bsvibe_cli(["pat", "create", "--name", "x"]) == 1
    err = capsys.readouterr().err
    assert "403" in err
    assert "mcp:admin" in err
