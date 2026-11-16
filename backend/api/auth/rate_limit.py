"""Abuse limits for the proxied sign-up / login / password-reset routes.

GoTrue sees only THIS backend's IP, so its own per-IP limit is shared by every user: one
caller looping on login locks everybody out, and repeated sign-up / reset requests can mass-send
mail. These limits give each client (and each mailbox) its own budget BEFORE GoTrue is called.

Two keys, per route: the client IP (``resolve_client_ip`` -- the header the trusted front proxy
SETS, never a client-appendable ``X-Forwarded-For``) and the normalised email (hashed, so the
address never lands in Redis keys).
"""

from __future__ import annotations

import hashlib
import unicodedata

from fastapi import HTTPException, Request, status

from backend.api.oauth import _SlidingWindowLimiter
from backend.config import get_settings
from backend.shared.client_ip import resolve_client_ip

_IP_WINDOW_SECS = 60
_EMAIL_WINDOW_SECS = 3600


class _AuthLimiters:
    """Lazily builds one limiter per (route, kind, budget) so settings changes take effect."""

    def __init__(self) -> None:
        self._limiters: dict[tuple[str, int, int], _SlidingWindowLimiter] = {}

    def get(self, name: str, window_secs: int, max_per_window: int) -> _SlidingWindowLimiter:
        key = (name, window_secs, max_per_window)
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = _SlidingWindowLimiter(
                name=f"auth_{name}_{window_secs}_{max_per_window}",
                window_secs=window_secs,
                max_per_window=max_per_window,
            )
            self._limiters[key] = limiter
        return limiter

    def reset(self) -> None:
        for limiter in self._limiters.values():
            limiter.reset()


_LIMITERS = _AuthLimiters()


def _reset_auth_rate_limits_for_tests() -> None:
    _LIMITERS.reset()


def normalize_email(email: str) -> str:
    """The identity of a mailbox for limiting: NFKC, trimmed, case-folded."""
    return unicodedata.normalize("NFKC", email).strip().casefold()


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="too many requests — please try again later",
        headers={"Retry-After": str(retry_after)},
    )


async def enforce_auth_rate_limit(request: Request, *, route: str, email: str) -> None:
    """Spend one unit of the IP and email budget for ``route``; raise 429 when either is out."""
    settings = get_settings()
    ip = resolve_client_ip(request, route=f"auth/{route}", header=settings.trusted_client_ip_header)
    ip_limiter = _LIMITERS.get(
        f"{route}_ip", _IP_WINDOW_SECS, settings.auth_rate_limit_ip_per_minute
    )
    if not await ip_limiter.check_and_record(ip):
        raise _too_many(_IP_WINDOW_SECS)
    digest = hashlib.sha256(normalize_email(email).encode()).hexdigest()
    email_limiter = _LIMITERS.get(
        f"{route}_email", _EMAIL_WINDOW_SECS, settings.auth_rate_limit_email_per_hour
    )
    if not await email_limiter.check_and_record(digest):
        raise _too_many(_EMAIL_WINDOW_SECS)
