"""The rate-limit key must come from the one hop a client cannot forge.

``backend.api.main`` mounts ``ProxyHeadersMiddleware(trusted_hosts="*")``.
In uvicorn 0.47 that sets ``always_trust``, and
``_TrustedHosts.get_trusted_client_address`` then returns the FIRST entry of
``X-Forwarded-For``. Cloudflare does not overwrite that header — it appends
the real peer after whatever the client sent — so the first entry, and
therefore ``request.client.host``, is attacker-chosen. Measured against the
deployed middleware:

===============================  =========================
``X-Forwarded-For``              resulting ``scope["client"]``
===============================  =========================
``203.0.113.9``                  ``('203.0.113.9', 0)``
``1.2.3.4, 203.0.113.9``         ``('1.2.3.4', 0)``
``9.9.9.9, 203.0.113.9``         ``('9.9.9.9', 0)``
``198.51.100.7, 203.0.113.9``    ``('198.51.100.7', 0)``
===============================  =========================

These tests pin the resolver that fixes it: prefer ``CF-Connecting-IP``
(which Cloudflare SETS, so a client cannot forge it), fall back to the
socket peer, and make the fallback observable.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from structlog.testing import capture_logs

from backend.shared.client_ip import (
    CF_CONNECTING_IP_HEADER,
    UNKNOWN_CLIENT_IP,
    resolve_client_ip,
)

ROUTE = "/api/oauth/token"


def _request(
    *,
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("10.0.0.5", 44321),
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": ROUTE,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


def test_cf_connecting_ip_wins_over_a_forged_forwarded_for_peer() -> None:
    """``scope["client"]`` here is what ProxyHeaders produced from a forged XFF."""
    request = _request(
        headers={
            CF_CONNECTING_IP_HEADER: "203.0.113.9",
            "X-Forwarded-For": "1.2.3.4, 203.0.113.9",
        },
        client=("1.2.3.4", 0),
    )
    assert resolve_client_ip(request, route=ROUTE) == "203.0.113.9"


def test_a_forged_first_entry_cannot_move_the_key() -> None:
    """Two different forged XFF heads behind ONE Cloudflare peer = one key."""
    forged = [
        "1.2.3.4, 203.0.113.9",
        "9.9.9.9, 203.0.113.9",
        "198.51.100.7, 203.0.113.9",
    ]
    keys = {
        resolve_client_ip(
            _request(
                headers={CF_CONNECTING_IP_HEADER: "203.0.113.9", "X-Forwarded-For": xff},
                # What ProxyHeadersMiddleware actually writes for that XFF.
                client=(xff.split(",")[0].strip(), 0),
            ),
            route=ROUTE,
        )
        for xff in forged
    }
    assert keys, "the forged-XFF fixture list must not be empty"
    assert keys == {"203.0.113.9"}


def test_falls_back_to_the_socket_peer_when_the_header_is_absent() -> None:
    request = _request(client=("10.0.0.5", 44321))
    assert resolve_client_ip(request, route=ROUTE) == "10.0.0.5"


def test_a_blank_header_is_treated_as_absent() -> None:
    request = _request(headers={CF_CONNECTING_IP_HEADER: "   "}, client=("10.0.0.5", 1))
    assert resolve_client_ip(request, route=ROUTE) == "10.0.0.5"


def test_the_header_value_is_stripped() -> None:
    request = _request(headers={CF_CONNECTING_IP_HEADER: " 203.0.113.9 "}, client=("1.2.3.4", 0))
    assert resolve_client_ip(request, route=ROUTE) == "203.0.113.9"


def test_no_header_and_no_peer_resolves_to_the_unknown_sentinel() -> None:
    request = _request(client=None)
    assert resolve_client_ip(request, route=ROUTE) == UNKNOWN_CLIENT_IP


def test_the_missing_header_is_logged_with_the_route() -> None:
    """A silently-absent header means the spoofable key is back — say so."""
    with capture_logs() as logs:
        resolve_client_ip(_request(client=("10.0.0.5", 1)), route=ROUTE)
    events = [entry for entry in logs if entry.get("event") == "client_ip.cf_header_missing"]
    assert events, f"expected a cf_header_missing log, got {logs!r}"
    assert events[0]["route"] == ROUTE
    assert events[0]["log_level"] == "warning"


def test_the_fallback_log_does_not_dump_the_header_set() -> None:
    """Log the fact plus the route — never the caller's headers."""
    with capture_logs() as logs:
        resolve_client_ip(
            _request(
                headers={
                    "X-Forwarded-For": "1.2.3.4, 203.0.113.9",
                    "Authorization": "Bearer super-secret",
                },
                client=("1.2.3.4", 0),
            ),
            route=ROUTE,
        )
    events = [entry for entry in logs if entry.get("event") == "client_ip.cf_header_missing"]
    assert events, f"expected a cf_header_missing log, got {logs!r}"
    rendered = repr(events[0])
    assert "super-secret" not in rendered
    assert "X-Forwarded-For" not in rendered


def test_a_present_header_logs_nothing() -> None:
    with capture_logs() as logs:
        resolve_client_ip(
            _request(headers={CF_CONNECTING_IP_HEADER: "203.0.113.9"}),
            route=ROUTE,
        )
    assert [entry for entry in logs if entry.get("event") == "client_ip.cf_header_missing"] == []
