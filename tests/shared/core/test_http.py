"""Tests for :class:`bsvibe_core.http.HttpClientBase`.

The base client is the shared HTTP foundation for all BSVibe outbound
calls (audit relay, central dispatch, …). It must:

* build httpx.AsyncClient lazily and own it iff caller didn't pass one
* inject ``Authorization`` / ``X-Service-Token`` headers when configured
* retry on ``httpx.HTTPError`` (network) and 5xx responses
* log every request via structlog without ever leaking token values
* expose ``clone(headers=...)`` for per-call header overlay
"""

from __future__ import annotations

from backend.shared.core.http import redact_url_password


class TestRedactUrlPassword:
    """A Redis/DSN URL carries its password in the userinfo. Once Redis has a
    ``requirepass`` the app's ``redis_url`` contains that secret, so every log
    site that echoed ``redis_url`` would leak it. ``redact_url_password`` masks
    the password while keeping the URL diagnosable.
    """

    def test_masks_password_keeps_user(self) -> None:
        out = redact_url_password("redis://:s3cr3t@redis:6379/0")
        assert "s3cr3t" not in out
        assert out == "redis://:<redacted>@redis:6379/0"

    def test_masks_password_with_username(self) -> None:
        out = redact_url_password("postgresql+asyncpg://bsvibe_app:pw@postgres:5432/bsvibe")
        assert "pw" not in out
        assert out == "postgresql+asyncpg://bsvibe_app:<redacted>@postgres:5432/bsvibe"
        # host / port / path / scheme survive so the log stays diagnosable.
        assert "postgres:5432" in out
        assert out.endswith("/bsvibe")

    def test_url_without_password_unchanged(self) -> None:
        assert redact_url_password("redis://redis:6379/0") == "redis://redis:6379/0"

    def test_url_with_user_only_unchanged(self) -> None:
        # A bare username (no ``:password``) carries no secret.
        assert (
            redact_url_password("redis://someuser@redis:6379/0") == "redis://someuser@redis:6379/0"
        )

    def test_empty_string_unchanged(self) -> None:
        assert redact_url_password("") == ""

    def test_non_url_garbage_returned_as_is(self) -> None:
        # Never raise on malformed input — logging must not blow up.
        assert redact_url_password("not a url") == "not a url"
