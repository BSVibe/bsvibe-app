"""Standardised structlog configuration for the BSVibe ecosystem.

Today the four products configure structlog three different ways:

* BSGateway uses ``stdlib.LoggerFactory`` + ``stdlib.BoundLogger``
  (`bsgateway/core/logging.py`).
* BSage uses ``PrintLoggerFactory`` + ``make_filtering_bound_logger``
  (`bsage/core/logging.py`).
* BSNexus uses *only* the stdlib ``logging`` module — no structlog.
* BSupervisor uses bare ``structlog.get_logger`` without an explicit
  ``configure()`` call.

The wire format the audit pipeline expects (timestamp ISO, level,
event, plus contextvars merge for request_id propagation) is identical
across the three that DO emit JSON. :func:`configure_logging` collapses
those three configurations into one.

Migration from each product after Phase A is mechanical:

.. code-block:: python

    # bsgateway/main.py / bsage/cli.py / bsupervisor/main.py / etc.
    from backend.shared.core import configure_logging

    configure_logging(level=settings.log_level, service_name="bsage")
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog

_NAME_TO_LEVEL: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return _NAME_TO_LEVEL.get(level.lower(), logging.INFO)


def _service_name_processor(service_name: str) -> Any:
    """Return a structlog processor that injects ``service`` on each event."""

    def _add_service(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict["service"] = service_name
        return event_dict

    return _add_service


def configure_logging(
    *,
    level: str | int = "info",
    json_output: bool = True,
    service_name: str | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Configure structlog with the BSVibe baseline pipeline.

    Parameters
    ----------
    level:
        Either a string name (``"info"``, ``"debug"``, ...) or a numeric
        :mod:`logging` level. Unknown strings fall back to ``INFO``.
    json_output:
        ``True`` (default) emits one JSON object per line — the production
        wire format. ``False`` switches to :class:`structlog.dev.ConsoleRenderer`
        for local development readability.
    service_name:
        Optional service identifier (``"bsage"``, ``"bsgateway"``, ...).
        When provided, every log line gets a ``service=<name>`` key.
        When ``None`` the field is omitted so the renderer stays minimal.
    stream:
        Optional output stream. Defaults to ``sys.stdout``. Tests pass a
        :class:`io.StringIO` to capture output deterministically — the
        stdlib ``capsys`` fixture does not interleave with structlog's
        :class:`PrintLoggerFactory` reliably across pytest versions.
    """

    numeric_level = _resolve_level(level)
    output_stream = stream if stream is not None else sys.stdout

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    if service_name is not None:
        processors.append(_service_name_processor(service_name))

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=output_stream),
        cache_logger_on_first_use=False,
    )


__all__ = ["configure_logging"]
