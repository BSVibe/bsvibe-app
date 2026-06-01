"""Producer-side stream emitter — best-effort, soft-fail, config-gated.

Workflow §12.5 #8 (Bundle G — Workers). When ``worker_mode="redis_streams"``
the producers that land a row a worker would otherwise poll ALSO emit a
notification onto the matching Redis Stream, so the consumer wakes immediately
instead of waiting for the next poll tick.

Hard invariants (the additive contract):

* The DB row is the **source of truth** — it is always written first, the
  XADD is only a wake-up notification. Losing the stream entry only delays a
  pickup until the next poll (DB-polling stays the safety net).
* Emission is **soft-fail**: a Redis hiccup must NEVER break the request path,
  so every error is swallowed (logged) and :func:`emit_stream_notification`
  returns ``False`` rather than raising.
* Emission is **gated**: a no-op (returns ``False``, never touches Redis) when
  ``worker_mode != "redis_streams"`` or no client is supplied — so the default
  DB-polling deployment behaves exactly as before.

Stream names are stable identifiers shared by producer + consumer:

* :data:`STREAM_INTAKE` — a TriggerEvent landed (intake produces a Request).
* :data:`STREAM_AGENT` — a Request is OPEN (the agent worker drives it).
* :data:`STREAM_DELIVER` — a delivery event landed (the delivery worker ships).
* :data:`STREAM_SETTLE` — a settle activity landed (the settle worker absorbs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import structlog

if TYPE_CHECKING:
    from backend.config import Settings

logger = structlog.get_logger(__name__)

STREAM_INTAKE = "intake"
STREAM_AGENT = "agent"
STREAM_DELIVER = "deliver"
STREAM_SETTLE = "settle"


class _RedisXadd(Protocol):
    """The single Redis method emission needs (any redis.asyncio / fakeredis
    client satisfies it). Narrowed so callers may inject a fake/None freely."""

    async def xadd(self, name: str, fields: dict[str, Any], **kwargs: Any) -> Any: ...


class _EmitClientCache:
    """Process-wide lazy client holder — Lift N defensive pattern #3.

    A small encapsulation so the *binding* is immutable (``Final``) and only
    the instance's internal slot mutates. Replaces the pre-Lift-N single-
    element ``list[X | None]`` module-level mutable; same semantics, cleaner
    boundary for the no-module-level-mutable invariant (v8 §22 #3 / D45).

    Producers that have no client of their own to inject (the HTTP routes:
    ``messages.py`` / ``webhooks.py``) acquire it via
    :func:`get_emit_redis_client`. The long-running worker daemon builds +
    owns its client explicitly in
    :mod:`backend.workflow.infrastructure.workers.run` instead (so it can
    ``aclose`` it on shutdown) — this cache is for the request-path producers
    that have no such lifecycle hook.
    """

    __slots__ = ("_client",)

    def __init__(self) -> None:
        self._client: _RedisXadd | None = None

    def get(self) -> _RedisXadd | None:
        return self._client

    def set(self, client: _RedisXadd | None) -> None:
        self._client = client

    def reset(self) -> None:
        self._client = None


_EMIT_CACHE: Final[_EmitClientCache] = _EmitClientCache()


def get_emit_redis_client(settings: Settings) -> _RedisXadd | None:
    """Return the process-wide emit client — built lazily, ONLY in redis mode.

    * ``worker_mode != "redis_streams"`` (the default DB-polling deployment):
      returns ``None`` WITHOUT importing redis or constructing a client, so the
      default path never touches Redis.
    * ``worker_mode == "redis_streams"``: builds a ``redis.asyncio`` client from
      ``settings.redis_url`` once (``decode_responses=True`` so stream fields are
      ``str``) and caches it for reuse across requests. Construction is
      connection-lazy (``redis.asyncio.from_url`` does not connect until the
      first command), so this never blocks; a Redis outage surfaces only at the
      :func:`emit_stream_notification` call, where it is swallowed (soft-fail).
    """
    if settings.worker_mode != "redis_streams":
        return None
    if _EMIT_CACHE.get() is None:
        import redis.asyncio as redis_aio  # noqa: PLC0415 — only imported in redis mode

        _EMIT_CACHE.set(
            cast("_RedisXadd", redis_aio.from_url(settings.redis_url, decode_responses=True))
        )
    return _EMIT_CACHE.get()


def reset_emit_redis_client() -> None:
    """Drop the cached emit client (test isolation hook)."""
    _EMIT_CACHE.reset()


async def emit_stream_notification(
    client: _RedisXadd | None,
    *,
    settings: Settings,
    stream: str,
    fields: dict[str, str],
) -> bool:
    """XADD ``fields`` onto ``stream`` — best-effort, soft-fail, gated.

    Returns ``True`` iff the entry was appended. Returns ``False`` (never
    raises) when gated off (``worker_mode != "redis_streams"`` or no client),
    or when Redis errors — the caller's DB write has already committed, so a
    failed notification is non-fatal and only logged.
    """
    if client is None or settings.worker_mode != "redis_streams":
        return False
    try:
        await client.xadd(stream, fields)
    except Exception:  # noqa: BLE001 — soft-fail: a Redis hiccup never breaks the request path
        logger.warning("stream_emit_failed", stream=stream, exc_info=True)
        return False
    return True


__all__ = [
    "STREAM_AGENT",
    "STREAM_DELIVER",
    "STREAM_INTAKE",
    "STREAM_SETTLE",
    "emit_stream_notification",
    "get_emit_redis_client",
    "reset_emit_redis_client",
]
