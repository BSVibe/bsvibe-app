"""Tests for bsvibe_fastapi.settings.FastApiSettings.

Pins the wire format extracted from BSupervisor PR #13 §M18:

* ``cors_allowed_origins`` is ``Annotated[list[str], NoDecode]`` so
  pydantic-settings does NOT JSON-decode the env value.
* A ``field_validator(mode="before")`` runs ``parse_csv_list`` to split
  on commas — the legacy ``os.environ.get(...).split(",")`` shape works
  unchanged.
* Empty / unset env falls back to a sane localhost default so dev
  bootstrap does not crash without ``.env`` configuration.
"""

from __future__ import annotations

import pytest

from backend.shared.fastapi.settings import FastApiSettings


class TestCorsAllowedOriginsNoDecode:
    """Wire-compatible with BSupervisor §M18 — comma-separated env vars."""

    def test_default_when_env_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
        s = FastApiSettings()
        assert s.cors_allowed_origins == ["http://localhost:3500"]

    def test_csv_env_is_split_not_json_decoded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Without NoDecode, pydantic-settings would try to JSON-parse this
        # and raise ValidationError. The M18 fix is exactly this.
        monkeypatch.setenv(
            "CORS_ALLOWED_ORIGINS",
            "http://a.test,http://b.test,http://c.test",
        )
        s = FastApiSettings()
        assert s.cors_allowed_origins == [
            "http://a.test",
            "http://b.test",
            "http://c.test",
        ]

    def test_empty_env_falls_back_to_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")
        s = FastApiSettings()
        assert s.cors_allowed_origins == ["http://localhost:3500"]

    def test_whitespace_and_empty_tokens_dropped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "CORS_ALLOWED_ORIGINS",
            "  http://a.test  , , http://b.test ,",
        )
        s = FastApiSettings()
        assert s.cors_allowed_origins == ["http://a.test", "http://b.test"]

    def test_case_insensitive_env_var(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("cors_allowed_origins", "http://x.test")
        s = FastApiSettings()
        assert s.cors_allowed_origins == ["http://x.test"]


class TestCorsExtraSettings:
    """Sane CORS defaults that match the four products' middleware."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORS_ALLOW_CREDENTIALS", raising=False)
        s = FastApiSettings()
        assert s.cors_allow_credentials is True
        assert "GET" in s.cors_allow_methods
        assert "POST" in s.cors_allow_methods
        assert "Authorization" in s.cors_allow_headers
        assert "Content-Type" in s.cors_allow_headers
        # Tier 3.2 — the SPA sends the active tenant as X-Active-Tenant.
        assert "X-Active-Tenant" in s.cors_allow_headers

    def test_credentials_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "false")
        s = FastApiSettings()
        assert s.cors_allow_credentials is False

    def test_methods_csv_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_METHODS", "GET,POST")
        s = FastApiSettings()
        assert s.cors_allow_methods == ["GET", "POST"]

    def test_headers_csv_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ALLOW_HEADERS", "Authorization,X-Tenant-Id")
        s = FastApiSettings()
        assert s.cors_allow_headers == ["Authorization", "X-Tenant-Id"]


class TestExtendsBsvibeSettings:
    """FastApiSettings must remain a drop-in BsvibeSettings."""

    def test_is_subclass_of_bsvibe_settings(self) -> None:
        from backend.shared.core import BsvibeSettings

        assert issubclass(FastApiSettings, BsvibeSettings)

    def test_unknown_env_vars_are_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # extra="ignore" inherited from BsvibeSettings; products may carry
        # arbitrary additional env vars without breaking startup.
        monkeypatch.setenv("BSVIBE_TOTALLY_UNKNOWN_FIELD", "x")
        FastApiSettings()  # must not raise
