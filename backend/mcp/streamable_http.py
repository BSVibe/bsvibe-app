"""Streamable HTTP transport for the embedded BSVibe MCP server — Lift D2.

The MCP server is mounted at ``/mcp``. Every incoming HTTP request is
authenticated up front (RFC 6750 Bearer token, verified against the
embedded OAuth server's JWKS + the access-token row) before the request
ever reaches the SDK's ``StreamableHTTPSessionManager``. The verified
principal is stashed on a context-var (:mod:`backend.mcp.principal`) so
the dispatcher reads it back into :class:`ToolContext` — the SDK's
``CallTool`` path does not thread HTTP headers down to handlers.

A missing / malformed / expired / revoked token gets a 401 response
that carries the RFC 6750 + RFC 9728 ``WWW-Authenticate`` header so
Claude Code (and any compliant MCP client) discovers the authorization
server via the resource-metadata document automatically.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.mcp.auth import McpAuthError, resolve_principal_from_bearer
from backend.mcp.principal import reset_request_principal, set_request_principal

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# RFC 6750 / 9728 401 helpers.
# ---------------------------------------------------------------------------
def _www_authenticate(issuer: str, error: str | None = None) -> str:
    """Build the ``WWW-Authenticate: Bearer ...`` header value."""
    metadata_url = f"{issuer.rstrip('/')}/api/.well-known/oauth-protected-resource"
    parts = [f'Bearer resource_metadata="{metadata_url}"']
    if error is not None:
        parts.append(f'error="{error}"')
    return ", ".join(parts)


async def _send_401(send: Callable[..., Any], issuer: str, error: str) -> None:
    """ASGI 401 response carrying RFC 6750 + RFC 9728 discovery headers."""
    headers = [
        (b"content-type", b"application/json"),
        (b"www-authenticate", _www_authenticate(issuer, error).encode("ascii")),
    ]
    body = b'{"error":"' + error.encode("ascii") + b'"}'
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


# ---------------------------------------------------------------------------
# ASGI app factory.
# ---------------------------------------------------------------------------
def build_streamable_http_app(
    *,
    issuer: str,
    session_factory: async_sessionmaker[Any],
    manager: StreamableHTTPSessionManager,
) -> Callable[..., Any]:
    """Return the ASGI app that authenticates + delegates to the SDK.

    The ``StreamableHTTPSessionManager`` must already have been entered
    (``async with manager.run()``) by a FastAPI lifespan before the first
    request arrives — the SDK creates its task group there and
    per-request handlers raise ``RuntimeError`` otherwise.
    """

    async def asgi_app(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # WebSocket / lifespan messages bypass the auth check; the SDK
            # treats them as transport-control frames.
            await manager.handle_request(scope, receive, send)
            return

        decoded = _decode_headers(scope.get("headers") or [])
        auth = decoded.get("authorization") or decoded.get("Authorization")
        if not auth:
            await _send_401(send, issuer, "invalid_token")
            return
        token = _strip_bearer(auth)
        if token is None:
            await _send_401(send, issuer, "invalid_token")
            return

        try:
            async with session_factory() as session:
                principal = await resolve_principal_from_bearer(
                    token=token, issuer=issuer, session=session
                )
        except McpAuthError as exc:
            await _send_401(send, issuer, exc.reason)
            return
        except Exception:  # noqa: BLE001 — never leak a 500 from the auth path
            logger.warning("mcp_auth_unexpected_failure", exc_info=True)
            await _send_401(send, issuer, "invalid_token")
            return

        token_handle = set_request_principal(principal)
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            reset_request_principal(token_handle)

    return asgi_app


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> Mapping[str, str]:
    """ASGI header decode — lowercased keys, latin-1 bytes."""
    out: dict[str, str] = {}
    for k, v in raw:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except (AttributeError, UnicodeDecodeError):  # pragma: no cover
            continue
    return out


def _strip_bearer(value: str) -> str | None:
    """Strip the ``Bearer `` prefix from an ``Authorization`` header value."""
    parts = value.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


__all__ = [
    "build_streamable_http_app",
]
