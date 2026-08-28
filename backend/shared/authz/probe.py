"""Active probe of the user-JWT verification dependency.

``verify_user_jwt`` resolves its key from a JWKS URL, a static public key, or a
symmetric secret. When that source is a REMOTE url, losing it takes out every
authenticated REST route and sign-in itself — and does so silently, because the
only passive signal is a real request failing, and a deployment nobody happens
to be signing into produces no such request.

Measured on prod 2026-08-28: the Supabase project backing ``USER_JWT_JWKS_URL``
was paused, which removes its subdomain from DNS entirely. Sign-in returned 500
and every ``get_current_user`` route 401'd — for weeks, unnoticed, while the
host uptime probe stayed green because it calls ANY HTTP response healthy.

Authentication only, no bounded-context imports: this stays a common leaf.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import jwt
import structlog

from .settings import Settings

logger = structlog.get_logger(__name__)

#: How long one probe may block. The caller is a polling worker, not a request,
#: so this is about not wedging the loop rather than user-perceived latency.
_PROBE_TIMEOUT_S = 10.0

KeySource = Literal["jwks_url", "public_key", "shared_secret", "unconfigured"]


@dataclass(frozen=True)
class UserKeySourceStatus:
    """Whether user-session JWTs can be verified right now.

    ``source`` names what was actually examined, so ``ok`` is never a claim
    about a check that did not happen: a deployment with no remote dependency
    reports its real source rather than an unearned ``jwks_url`` pass.
    """

    ok: bool
    source: KeySource
    detail: str | None = None


def _fetch_jwks(url: str) -> None:
    jwt.PyJWKClient(url).get_jwk_set()


async def check_user_key_source(settings: Settings) -> UserKeySourceStatus:
    """Verify the configured key source is usable. Never raises.

    A fresh client per call on purpose — a cached one would answer from the key
    set it fetched before the outage and report a dead dependency as healthy.
    """
    if settings.user_jwt_jwks_url:
        url = settings.user_jwt_jwks_url
        try:
            await asyncio.wait_for(asyncio.to_thread(_fetch_jwks, url), _PROBE_TIMEOUT_S)
        except TimeoutError:
            return UserKeySourceStatus(
                ok=False, source="jwks_url", detail=f"timed out after {_PROBE_TIMEOUT_S}s"
            )
        except Exception as exc:  # noqa: BLE001 — a polling caller must survive anything
            return UserKeySourceStatus(ok=False, source="jwks_url", detail=str(exc))
        return UserKeySourceStatus(ok=True, source="jwks_url")

    if settings.user_jwt_algorithm == "HS256":
        if settings.user_jwt_secret:
            return UserKeySourceStatus(ok=True, source="shared_secret")
        return UserKeySourceStatus(
            ok=False, source="unconfigured", detail="user_jwt_secret not configured"
        )

    if settings.user_jwt_public_key:
        return UserKeySourceStatus(ok=True, source="public_key")

    return UserKeySourceStatus(
        ok=False,
        source="unconfigured",
        detail="user_jwt_public_key or user_jwt_jwks_url not configured",
    )


__all__ = ["KeySource", "UserKeySourceStatus", "check_user_key_source"]
