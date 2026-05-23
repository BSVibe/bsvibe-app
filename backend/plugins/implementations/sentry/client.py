"""Thin async Sentry REST client built on httpx.

Deliberately small — only the calls the sentry plugin needs (update an issue's
status: resolve / unresolve). No Sentry SDK is used (keeps the dependency
surface to httpx, already a project dep).

Sentry uses ordinary REST semantics: a non-2xx response is a hard failure.
Unlike Slack, there is no ``{"ok": false}`` 200 body — :meth:`SentryClient._json`
maps any non-2xx to :class:`SentryApiError` (preserving the status code so the
compensate handler can treat an already-gone issue as an idempotent no-op).

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://sentry.io/api/0"


class SentryApiError(RuntimeError):
    """Raised when the Sentry REST API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"sentry api error {status_code}: {detail}")


class SentryClient:
    """Authenticated wrapper over the Sentry REST API (Bearer auth token)."""

    def __init__(
        self,
        auth_token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = auth_token
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            return await self._client.request(method, url, headers=self._headers(), json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, headers=self._headers(), json=json_body)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        """Map any non-2xx to :class:`SentryApiError`; else return the JSON body."""
        if resp.status_code >= 400:
            raise SentryApiError(resp.status_code, resp.text)
        body: dict[str, Any] = resp.json()
        return body

    # ── issues ───────────────────────────────────────────────────────────────

    async def update_issue_status(self, issue_id: str, status: str) -> dict[str, Any]:
        """Set an issue's status (e.g. ``resolved`` / ``unresolved``).

        ``PUT /issues/{issue_id}/`` with ``{"status": status}``.
        """
        resp = await self._request("PUT", f"/issues/{issue_id}/", json_body={"status": status})
        return self._json(resp)


__all__ = ["DEFAULT_BASE_URL", "SentryApiError", "SentryClient"]
