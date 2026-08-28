"""Active probe of the user-JWT verification dependency.

Prod 2026-08-28: the Supabase project backing ``USER_JWT_JWKS_URL`` was paused,
which removes its subdomain from DNS entirely (NXDOMAIN — confirmed from both
the host and the container while every other name resolved). Sign-in returned
500 and every ``get_current_user`` route 401'd with "JWKS resolution failed".

Nothing caught it. The host uptime probe calls the deployment healthy on ANY
HTTP response, so a total auth outage reads as green, and the founder had been
working through MCP (a different issuer, verified against local keys) for weeks.
A failure nobody can see is the failure.

This probe is ACTIVE on purpose: the passive signal — a real request failing —
only exists when someone tries, and nobody did.
"""

from __future__ import annotations

import jwt
import pytest

from backend.shared.authz.probe import UserKeySourceStatus, check_user_key_source
from backend.shared.authz.settings import Settings

pytestmark = pytest.mark.asyncio


def _jwks_settings(url: str) -> Settings:
    return Settings(user_jwt_jwks_url=url, user_jwt_algorithm="ES256")


async def test_unreachable_jwks_reports_down_with_the_reason(monkeypatch) -> None:
    def _boom(self, *a, **k):  # noqa: ANN001, ARG001
        raise jwt.PyJWKClientError("Name or service not known")

    monkeypatch.setattr(jwt.PyJWKClient, "get_jwk_set", _boom, raising=False)

    status = await check_user_key_source(_jwks_settings("https://gone.example.invalid/jwks.json"))

    assert status.ok is False
    assert status.source == "jwks_url"
    assert "Name or service not known" in (status.detail or "")


async def test_reachable_jwks_reports_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        jwt.PyJWKClient, "get_jwk_set", lambda self, *a, **k: object(), raising=False
    )

    status = await check_user_key_source(_jwks_settings("https://live.example.test/jwks.json"))

    assert status.ok is True
    assert status.source == "jwks_url"


async def test_a_deployment_without_a_jwks_url_is_not_reported_as_down() -> None:
    """A static/HS256 deployment has no remote dependency to lose.

    Reporting it "down" would cry wolf; reporting it "ok" while naming the
    JWKS source would claim a check that never happened. It names its real
    source instead — the honest-absence rule.
    """
    status = await check_user_key_source(
        Settings(user_jwt_algorithm="HS256", user_jwt_secret="s3cret")
    )

    assert status.ok is True
    assert status.source == "shared_secret"


async def test_a_deployment_with_no_key_material_at_all_is_down() -> None:
    """Nothing configured cannot verify anyone — that IS an auth outage."""
    status = await check_user_key_source(Settings(user_jwt_algorithm="ES256"))

    assert status.ok is False
    assert status.source == "unconfigured"


async def test_probe_never_raises_into_its_caller(monkeypatch) -> None:
    """It runs inside a polling worker; an exception here must not kill the loop."""

    def _explode(self, *a, **k):  # noqa: ANN001, ARG001
        raise RuntimeError("unexpected")

    monkeypatch.setattr(jwt.PyJWKClient, "get_jwk_set", _explode, raising=False)

    status = await check_user_key_source(_jwks_settings("https://x.example.test/jwks.json"))

    assert isinstance(status, UserKeySourceStatus)
    assert status.ok is False
