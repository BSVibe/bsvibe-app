"""Process-wide Redis client accessor for the API container.

The HTTP container already builds a Redis client at startup, but before this
module it was bound to ONE consumer only — the live-event bus
(:func:`backend.api.v1.live_events.set_live_event_bus_redis`) — and was
therefore unreachable from request handlers.

That gap was a production bug. Request handlers that resolve a ModelAccount
(:class:`backend.dispatch.resolver.ModelAccountResolver`) must thread a Redis
client through, because an **executor** account's adapter
(:class:`backend.dispatch.adapter.ExecutorAdapter`) dispatches onto the worker
stream via Redis and raises
:class:`~backend.dispatch.adapter.ExecutorAdapterUnavailable` without one. The
workflow runtime does thread it (see
:func:`backend.workflow.application.runtime.account_resolution._resolve_via_caller`);
the API layer did not. Every NL routing compile (``/api/v1/run-routing/compile``,
``/compile/apply``, the ``source_text`` compile on rule create/update) and the
external ``/api/v1/chat/completions`` gateway therefore failed on any workspace
whose routed account is an executor — which is the default for a founder running
claude_code.

The accessor mirrors the module-singleton + explicit wire-up seam pattern that
:mod:`backend.api.v1.live_events` already uses, for the same reason: the client
is created once per process at startup, and the consumers are plain functions
(shared by REST *and* MCP handlers), not FastAPI-dependency-injectable objects.

Wire-up: :func:`backend.api.main.bind_process_redis` calls :func:`set_api_redis`
once at app start. Nothing configured (no ``redis_url``, or the pytest guard) →
the accessor stays ``None`` and every consumer degrades exactly as it did before
— an executor account then raises ``ExecutorAdapterUnavailable`` at chat() time,
which the callers now surface as a "couldn't reach the model" 502 rather than
blaming the founder's phrasing.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Typed ``Any`` rather than a Protocol for the same reason
# :mod:`backend.api.v1.live_events` does: the real ``redis.asyncio.Redis`` has
# wide overloaded signatures that don't structurally satisfy a narrow Protocol
# without a cast at every wire-up site. ``backend.dispatch.resolver`` /
# ``backend.dispatch.adapter`` already take the client as ``Any``.
_REDIS: Any | None = None


def set_api_redis(redis: Any | None) -> None:
    """Publish (or clear, with ``None``) the process-wide API Redis client.

    Called once at app startup by :func:`backend.api.main.bind_process_redis`.
    Tests call it directly to inject a stub / reset the singleton.
    """
    global _REDIS  # noqa: PLW0603 — process-wide singleton, same as the live-event bus
    _REDIS = redis


def get_api_redis() -> Any | None:
    """The process-wide Redis client, or ``None`` when none is configured.

    ``None`` is a legitimate, non-crashing state (unit tests, dev without Redis):
    LiteLLM-backed accounts never touch Redis, and an executor-backed account
    raises a clean ``ExecutorAdapterUnavailable`` the callers map to a 502.
    """
    return _REDIS


def reset_api_redis_for_testing() -> None:
    """Drop the singleton so a test starts from a known state."""
    set_api_redis(None)


__all__ = ["get_api_redis", "reset_api_redis_for_testing", "set_api_redis"]
