"""Shared async HTTP client foundation for the BSVibe ecosystem.

Every outbound HTTP call (audit relay, central dispatch, future CLI
clients) flows through :class:`HttpClientBase`. The class centralises
four cross-cutting concerns that were previously copy-pasted across
several hand-rolled clients:

* httpx.AsyncClient lifecycle (lazy build, ownership tracking)
* Authorization / X-Service-Token header injection
* Retry on network errors and 5xx responses
* Structured logging that NEVER includes credential values

Subclasses (``AuditClient``, ``CentralDispatchClient``, …) build
endpoint-specific helpers on top of :meth:`HttpClientBase.request`
instead of re-implementing any of the above.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import structlog

logger = structlog.get_logger(__name__)

_REDACTED_HEADER_NAMES: frozenset[str] = frozenset({"authorization", "x-service-token"})
_REDACTED_PLACEHOLDER = "<redacted>"


def redact_url_password(url: str) -> str:
    """Return ``url`` with the userinfo password masked.

    Connection URLs (Redis ``redis://:pw@host``, Postgres DSNs) carry their
    secret in the userinfo component. Once Redis runs with ``requirepass`` the
    app's ``redis_url`` embeds that password, so any log site that echoes the
    URL would leak it. This masks the password (``:pw@`` → ``:<redacted>@``)
    while leaving scheme / user / host / port / path intact for diagnostics.

    Never raises: malformed input is returned unchanged so logging cannot blow
    up on a bad URL.
    """

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password is None:
        return url
    # Rebuild the netloc with the password replaced. ``parts.hostname`` lowercases
    # and strips brackets, so reconstruct from the raw netloc to preserve it.
    userinfo, _, hostinfo = parts.netloc.rpartition("@")
    user, _, _pw = userinfo.partition(":")
    new_netloc = f"{user}:{_REDACTED_PLACEHOLDER}@{hostinfo}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


__all__ = ["redact_url_password"]
