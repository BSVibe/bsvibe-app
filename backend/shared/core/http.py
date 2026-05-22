"""Shared async HTTP client foundation for the BSVibe ecosystem.

Every outbound HTTP call (OpenFGA, audit relay, central dispatch,
IdP introspection, future CLI clients) flows through
:class:`HttpClientBase`. The class centralises four cross-cutting
concerns that were previously copy-pasted across ~5 hand-rolled
clients:

* httpx.AsyncClient lifecycle (lazy build, ownership tracking)
* Authorization / X-Service-Token header injection
* Retry on network errors and 5xx responses
* Structured logging that NEVER includes credential values

Subclasses (``OpenFGAClient``, ``AuditClient``, ``CentralDispatchClient``,
introspection / device-flow clients) build endpoint-specific helpers on
top of :meth:`HttpClientBase.request` instead of re-implementing any of
the above.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_REDACTED_HEADER_NAMES: frozenset[str] = frozenset({"authorization", "x-service-token"})
_REDACTED_PLACEHOLDER = "<redacted>"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential values masked.

    Headers matched case-insensitively against
    ``Authorization`` and ``X-Service-Token``. The original mapping is
    not mutated.
    """

    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _REDACTED_HEADER_NAMES:
            out[key] = _REDACTED_PLACEHOLDER
        else:
            out[key] = value
    return out


class HttpClientBase:
    """Shared async HTTP client.

    Parameters
    ----------
    base_url:
        Default base URL for relative paths. Used both when building the
        owned httpx client and when logging requests.
    http:
        Optional pre-built ``httpx.AsyncClient``. When provided the
        caller retains ownership — :meth:`aclose` will NOT close it.
        When omitted, the client is built lazily on first use and owned
        by this instance.
    timeout_s:
        Per-request timeout applied when building the owned client.
        Ignored when ``http`` is supplied — the caller's timeout wins.
    retries:
        Number of retry attempts (in addition to the first try) on
        network errors and 5xx responses. Default 2 → up to 3 attempts.
    headers:
        Default headers merged into every request. Per-call ``headers``
        override these.
    """

    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        retries: int = 2,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._retries = retries
        self._headers: dict[str, str] = dict(headers) if headers else {}
        self._http: httpx.AsyncClient | None = http
        self._owns_http: bool = http is None
        self._transport = transport

    @property
    def http(self) -> httpx.AsyncClient:
        """Underlying ``httpx.AsyncClient``, built lazily on first access."""

        if self._http is None:
            kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "timeout": self._timeout_s,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    @property
    def retries(self) -> int:
        return self._retries

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    def clone(self, *, headers: Mapping[str, str] | None = None) -> HttpClientBase:
        """Return a sibling client sharing the same httpx.AsyncClient.

        The clone overlays additional default ``headers`` on top of this
        instance's defaults. Useful for per-tenant or per-call header
        scoping without rebuilding the connection pool. The clone never
        owns the underlying client — :meth:`aclose` on it is a no-op.
        """

        merged = {**self._headers, **(dict(headers) if headers else {})}
        clone = type(self).__new__(type(self))
        HttpClientBase.__init__(
            clone,
            self._base_url,
            http=self.http,
            timeout_s=self._timeout_s,
            retries=self._retries,
            headers=merged,
        )
        clone._owns_http = False
        return clone

    async def __aenter__(self) -> HttpClientBase:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        content: Any | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: Any | None = None,
    ) -> httpx.Response:
        """Issue a request with retry + structured logging.

        Retries are applied on:

        * ``httpx.HTTPError`` (network failure, timeout, etc.)
        * 5xx responses

        4xx responses are returned to the caller without retry — they
        represent client-side errors that retrying will not fix. The
        final exception (after exhausted retries) is re-raised verbatim
        so callers can pattern-match on httpx exception classes.

        Dispatches via the method-specific ``httpx.AsyncClient`` helper
        (``post``/``get``/...) so test suites that monkey-patch those
        attributes (rather than the underlying transport) keep working.
        """

        merged_headers = {**self._headers, **(dict(headers) if headers else {})}
        log_headers = redact_headers(merged_headers)
        max_attempts = self._retries + 1
        last_error: BaseException | None = None
        method_attr = method.lower()

        for attempt in range(max_attempts):
            start = time.monotonic()
            try:
                http_method = getattr(self.http, method_attr)
                call_kwargs: dict[str, Any] = {
                    "params": params,
                    "headers": merged_headers,
                    "auth": auth,
                }
                if json is not None:
                    call_kwargs["json"] = json
                if data is not None:
                    call_kwargs["data"] = data
                if content is not None:
                    call_kwargs["content"] = content
                resp: httpx.Response = await http_method(path, **call_kwargs)
            except httpx.HTTPError as exc:
                duration_ms = (time.monotonic() - start) * 1000
                last_error = exc
                is_last = attempt == max_attempts - 1
                event = "http_request_error" if is_last else "http_request_retry"
                logger.warning(
                    event,
                    method=method,
                    path=path,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    duration_ms=round(duration_ms, 2),
                    headers=log_headers,
                )
                if is_last:
                    raise
                continue

            duration_ms = (time.monotonic() - start) * 1000
            is_last = attempt == max_attempts - 1
            if 500 <= resp.status_code < 600 and not is_last:
                logger.warning(
                    "http_request_retry",
                    method=method,
                    path=path,
                    status=resp.status_code,
                    attempt=attempt,
                    duration_ms=round(duration_ms, 2),
                    headers=log_headers,
                )
                continue

            logger.info(
                "http_request",
                method=method,
                path=path,
                status=resp.status_code,
                attempt=attempt,
                duration_ms=round(duration_ms, 2),
                headers=log_headers,
            )
            return resp

        # Loop exits only via return or raise; this is unreachable but
        # keeps mypy/typecheckers honest.
        assert last_error is not None  # pragma: no cover
        raise last_error  # pragma: no cover

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kw)


__all__ = ["HttpClientBase", "redact_headers"]
