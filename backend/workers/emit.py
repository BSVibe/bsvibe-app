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

from typing import TYPE_CHECKING, Any, Protocol

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
]
