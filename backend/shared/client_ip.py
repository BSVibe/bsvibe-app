"""Caller-IP resolution for abuse limits (neutral shared-kernel leaf).

Any per-IP limit is only as good as its key. The obvious key —
``request.client.host`` — is **attacker-controlled** on this deployment, and
that is not a bug in any one route: it is a property of the proxy stack.

``backend.api.main`` mounts ``ProxyHeadersMiddleware(trusted_hosts="*")`` so
``X-Forwarded-Proto`` is honoured and URL builders emit ``https://`` behind
Cloudflare. In uvicorn 0.47 ``trusted_hosts="*"`` sets ``always_trust``, and
``_TrustedHosts.get_trusted_client_address`` then returns
``x_forwarded_for_hosts[0]`` — the FIRST entry. Cloudflare does not overwrite
``X-Forwarded-For``; it APPENDS its view of the peer to whatever the client
sent. So the first entry is whatever the caller typed. Measured by running the
deployed middleware against uvicorn 0.47.0:

======================================  =============================
``X-Forwarded-For``                     resulting ``scope["client"]``
======================================  =============================
``203.0.113.9``                         ``('203.0.113.9', 0)``
``1.2.3.4, 203.0.113.9``                ``('1.2.3.4', 0)``
``9.9.9.9, 203.0.113.9``                ``('9.9.9.9', 0)``
``198.51.100.7, 203.0.113.9``           ``('198.51.100.7', 0)``
======================================  =============================

One header therefore bought a fresh bucket per request *and* let a caller
spend a chosen victim's budget. ``CF-Connecting-IP`` is the fix: Cloudflare
**sets** (overwrites) that header on every request it proxies, so a client
cannot forge it, and the only ingress to this origin is a Cloudflare Tunnel
(``cloudflared ... tunnel run``) — there is no non-CF path that could supply
it.

The proxy config itself is deliberately left alone: ``trusted_hosts="*"`` is
load-bearing for proto handling, and narrowing it would mean tracking
Cloudflare's egress ranges. Only the *key* moves.

This module imports nothing from any bounded context, so it satisfies the
"common leaves do not import bounded contexts" import-linter contract that
guards ``backend.shared``.
"""

from __future__ import annotations

import structlog
from starlette.requests import Request

logger = structlog.get_logger(__name__)

#: Set (not appended) by Cloudflare on every proxied request. Unforgeable from
#: outside because the tunnel is the only ingress.
CF_CONNECTING_IP_HEADER = "CF-Connecting-IP"

#: What a request with neither the header nor a socket peer resolves to. A real
#: key, so such requests still share ONE bucket rather than bypassing limits.
UNKNOWN_CLIENT_IP = "unknown"


def resolve_client_ip(request: Request, *, route: str) -> str:
    """Return the rate-limit key for ``request``.

    Prefers :data:`CF_CONNECTING_IP_HEADER`. Falls back to the socket peer
    (``request.client.host``) when the header is absent — dev, tests, and any
    direct call — which is exactly today's behaviour, so nothing regresses.

    The fallback is **logged**. A silently-absent header would mean the
    spoofable key is back on a rate-limited route and nobody would know. Only
    the fact and the route are logged, never the caller's headers.
    """
    forwarded = request.headers.get(CF_CONNECTING_IP_HEADER)
    if forwarded is not None:
        candidate = forwarded.strip()
        if candidate:
            return candidate
    logger.warning("client_ip.cf_header_missing", route=route, header=CF_CONNECTING_IP_HEADER)
    return request.client.host if request.client else UNKNOWN_CLIENT_IP


__all__ = [
    "CF_CONNECTING_IP_HEADER",
    "UNKNOWN_CLIENT_IP",
    "resolve_client_ip",
]
