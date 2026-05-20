"""Tests for the structlog baseline configuration.

The 4 BSVibe products today configure structlog three different ways
(BSGateway: stdlib factory; BSage: PrintLoggerFactory + filtering bound
logger; BSNexus: stdlib logging only). The new shared ``configure_logging``
must subsume the JSON shape they all use so migration is mechanical.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from backend.shared.core.logging import configure_logging


@pytest.fixture
def captured_stdout(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Replace the structlog print stream with a capture buffer.

    structlog's PrintLoggerFactory writes to sys.stdout by default;
    ``capsys`` does not interleave with the bound logger reliably across
    pytest versions, so we hand the factory an explicit StringIO.
    """
    buf = io.StringIO()
    yield buf
    buf.close()


class TestConfigureLogging:
    def test_returns_none(self) -> None:
        assert configure_logging() is None

    def test_default_level_is_info(self) -> None:
        configure_logging()
        # ``configure_logging`` installs a filtering bound logger.
        # The wrapper class is constructed with the chosen level —
        # we assert via behaviour rather than internals: a debug call
        # at default level must be a no-op, info must produce output.
        buf = io.StringIO()
        configure_logging(level="info", stream=buf)
        log = structlog.get_logger("test")
        log.debug("debug_event", key="value")
        log.info("info_event", key="value")

        output = buf.getvalue()
        assert "info_event" in output
        assert "debug_event" not in output

    def test_explicit_debug_level_emits_debug(self) -> None:
        buf = io.StringIO()
        configure_logging(level="debug", stream=buf)
        log = structlog.get_logger("test")
        log.debug("debug_event", key="value")

        assert "debug_event" in buf.getvalue()

    def test_unknown_level_falls_back_to_info(self) -> None:
        buf = io.StringIO()
        configure_logging(level="nonsense", stream=buf)
        log = structlog.get_logger("test")
        log.info("emitted", k=1)
        assert "emitted" in buf.getvalue()

    def test_json_renderer_is_default(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        log.info("event_name", key="value", count=3)

        line = buf.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["event"] == "event_name"
        assert record["key"] == "value"
        assert record["count"] == 3

    def test_iso_timestamp_present(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        log.info("ts_event")

        record = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert "timestamp" in record
        # ISO 8601 (T separator + Z or offset)
        assert "T" in record["timestamp"]

    def test_log_level_recorded(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        log.warning("warn_event")

        record = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert record["level"] == "warning"

    def test_service_name_injected_when_provided(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf, service_name="bsage")
        log = structlog.get_logger("svc")
        log.info("svc_event")

        record = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert record["service"] == "bsage"

    def test_service_name_omitted_when_not_provided(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        log.info("svc_event")

        record = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert "service" not in record

    def test_contextvars_bound_values_appear(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id="req-123")
        try:
            log = structlog.get_logger("svc")
            log.info("ctx_event")

            record = json.loads(buf.getvalue().strip().splitlines()[-1])
            assert record["request_id"] == "req-123"
        finally:
            structlog.contextvars.clear_contextvars()

    def test_console_renderer_is_human_readable(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf, json_output=False)
        log = structlog.get_logger("svc")
        log.info("readable_event", key="value")

        out = buf.getvalue()
        assert "readable_event" in out
        # Console renderer is NOT JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.strip().splitlines()[-1])

    def test_json_output_default_true(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        log.info("default_event")
        # Last line must parse as JSON (json_output=True is the default)
        json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_exception_info_rendered(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        log = structlog.get_logger("svc")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.error("exc_event", exc_info=True)

        record = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert "exception" in record
        assert "RuntimeError" in record["exception"]


class TestConfigureLoggingNumericLevel:
    def test_accepts_numeric_level(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf, level=logging.WARNING)
        log = structlog.get_logger("svc")
        log.info("info_event")
        log.warning("warn_event")

        out = buf.getvalue()
        assert "warn_event" in out
        assert "info_event" not in out
