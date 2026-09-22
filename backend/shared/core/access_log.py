"""Keep credentials out of the HTTP access log (#1027).

Some endpoints legitimately carry a secret in the URL. ``GET
/api/v1/events/stream?token=<session JWT>`` is the standing example: an
``EventSource`` cannot send an ``Authorization`` header, so the token rides in
a query parameter (``eventsource-sse-auth-trap``). That is the design. Writing
the URL to the access log verbatim is not: measured in production on
2026-09-21, a **still-valid** Supabase session JWT — carrying the founder's
email, ``sub`` and provider ids — sat in ``docker logs`` under a ``json-file``
driver with no ``max-size``, i.e. kept forever.

**Every value is redacted; the keys survive.** Listing the parameter names that
look sensitive (``token``, ``api_key``, ``secret``, …) only encodes the
spellings someone thought of, and the next endpoint to put a credential in a
query string would not be on the list
(``absence-guard-listing-spellings-proves-only-imagination``). Redacting all
values needs no list and cannot be outgrown. Keeping the keys costs nothing —
*which* parameters a request carried is useful for debugging and is not the
secret.

The path, method, status and client address are untouched.
"""

from __future__ import annotations

import logging

REDACTED = "***"

#: uvicorn's access record is ``(client_addr, method, full_path, http_version, status)``.
_PATH_ARG_INDEX = 2


def _redact_query(full_path: str) -> str:
    """Return ``full_path`` with every query-parameter VALUE replaced.

    Splitting is done on the FIRST ``?`` and then on ``&`` / the first ``=`` of
    each pair, so a value containing ``=`` (base64 padding, for one) is
    redacted whole rather than leaving its tail in the log.
    """
    head, sep, query = full_path.partition("?")
    if not sep or not query:
        return full_path
    pairs = []
    for pair in query.split("&"):
        key, eq, _value = pair.partition("=")
        pairs.append(f"{key}={REDACTED}" if eq else key)
    return f"{head}?{'&'.join(pairs)}"


class RedactQueryValuesFilter(logging.Filter):
    """Rewrite the path in a uvicorn access record so no value reaches the log.

    A :class:`logging.Filter` rather than a formatter: it applies wherever the
    record goes (stdout, a file, a shipper) instead of to one handler's layout,
    and a handler added later cannot quietly bypass it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > _PATH_ARG_INDEX:
            path = args[_PATH_ARG_INDEX]
            if isinstance(path, str) and "?" in path:
                mutated = list(args)
                mutated[_PATH_ARG_INDEX] = _redact_query(path)
                record.args = tuple(mutated)
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to ``uvicorn.access``. Idempotent."""
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, RedactQueryValuesFilter) for f in logger.filters):
        return
    logger.addFilter(RedactQueryValuesFilter())


__all__ = [
    "REDACTED",
    "RedactQueryValuesFilter",
    "install_access_log_redaction",
]
