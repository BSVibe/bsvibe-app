"""The access log must not become a credential store (#1027).

``GET /api/v1/events/stream?token=<session JWT>`` puts a live credential in the
URL on purpose — EventSource cannot send headers (``eventsource-sse-auth-trap``).
That design is fine; writing the URL to the access log verbatim is not. Measured
in production on 2026-09-21: a **still-valid** Supabase session JWT (with the
founder's email, ``sub`` and provider ids) sat in ``docker logs``, whose driver
had no ``max-size`` — unbounded retention.

**Why values and not a name list.** Redacting parameters called ``token`` /
``api_key`` / ``secret`` only proves what spellings I happened to think of
(``absence-guard-listing-spellings-proves-only-imagination``). Redacting EVERY
value needs no list and cannot be outgrown: a future endpoint that puts a
credential in a query parameter is covered the day it ships. The KEYS survive,
so the log still says which parameters a request carried.
"""

from __future__ import annotations

import logging

from backend.shared.core.access_log import (
    RedactQueryValuesFilter,
    install_access_log_redaction,
)

# A shape that is unmistakably a credential if it survives.
_JWT = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl"


def _record(path: str) -> logging.LogRecord:
    """A record shaped like uvicorn's access log: args[2] is the full path."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:0", "GET", path, "1.1", 200),
        exc_info=None,
    )


def _filtered(path: str) -> str:
    rec = _record(path)
    assert RedactQueryValuesFilter().filter(rec) is True, "the record must still be logged"
    return rec.getMessage()


def test_the_sse_token_does_not_survive() -> None:
    """The exact production line that leaked."""
    out = _filtered(f"/api/v1/events/stream?token={_JWT}")

    assert _JWT not in out
    assert "/api/v1/events/stream" in out, "the path itself is not a secret"
    assert "token=" in out, "which parameter was sent is useful and not sensitive"


def test_every_value_is_redacted_not_a_list_of_names() -> None:
    """A parameter nobody thought to name must be covered too."""
    out = _filtered("/x?limit=50&some_future_credential=hunter2&q=hello")

    assert "hunter2" not in out
    assert "50" not in out
    assert "hello" not in out
    assert "limit=" in out and "some_future_credential=" in out and "q=" in out


def test_a_path_without_a_query_is_untouched() -> None:
    assert "/api/v1/products" in _filtered("/api/v1/products")


def test_a_valueless_parameter_survives_as_itself() -> None:
    # The value is a distinctive string on purpose: asserting ``"1" not in out``
    # would fail on the record's own ``HTTP/1.1``, i.e. on something the filter
    # does not guard.
    out = _filtered("/x?flag&other=distinctivevalue")
    assert "flag" in out
    assert "distinctivevalue" not in out
    assert "other=" in out


def test_a_secret_containing_an_ampersand_or_equals_is_fully_redacted() -> None:
    """Naive splitting leaks the tail of a value that contains '=' (base64 pad)."""
    out = _filtered("/x?token=abc=def&next=zz")
    assert "abc" not in out and "def" not in out and "zz" not in out


def test_the_record_is_not_mangled_when_args_are_not_uvicorns_shape() -> None:
    """Other loggers share the name space; the filter must never raise."""
    rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message with no args",
        args=None,
        exc_info=None,
    )
    assert RedactQueryValuesFilter().filter(rec) is True
    assert rec.getMessage() == "plain message with no args"


def test_install_is_idempotent() -> None:
    """create_app() runs per-process but tests build many apps."""
    logger = logging.getLogger("uvicorn.access")
    before = len(logger.filters)
    install_access_log_redaction()
    install_access_log_redaction()
    added = len(logger.filters) - before
    assert added == 1, f"expected exactly one filter, added {added}"


def test_creating_the_app_installs_it() -> None:
    """The guard has to be wired, not merely available (a filter nobody
    installs is a module, not a defence)."""
    logger = logging.getLogger("uvicorn.access")
    logger.filters = [f for f in logger.filters if not isinstance(f, RedactQueryValuesFilter)]

    from backend.api.main import create_app

    create_app()

    assert any(isinstance(f, RedactQueryValuesFilter) for f in logger.filters)
