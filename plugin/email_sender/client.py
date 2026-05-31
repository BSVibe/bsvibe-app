"""Thin async Resend transactional-email client built on httpx.

Deliberately small — only the single call the email_sender plugin needs
(send one email). The official ``resend`` SDK is intentionally NOT used (keeps
the dependency surface to httpx, already a project dep), mirroring
:mod:`plugin.notion.client`.

Resend authenticates with a simple ``Authorization: Bearer <api_key>`` header
(no OAuth, no SMTP). Unlike Slack's Web API, Resend signals failure with the
HTTP status code (non-2xx) rather than a ``200 + {"ok": false}`` body, so a
plain ``raise_for_status``-style check is sufficient. On success it returns
``{"id": "..."}``.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.resend.com"


class EmailApiError(RuntimeError):
    """Raised when the Resend API returns a non-2xx response.

    Carries the HTTP ``status`` code and the best-effort error ``message``
    parsed from the response body so callers can log / surface a useful reason
    without leaking the full payload.
    """

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"resend api error: {status} {message}")


class ResendClient:
    """Authenticated wrapper over the Resend transactional-email API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, json_body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            return await self._client.post(url, headers=self._headers(), json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, headers=self._headers(), json=json_body)

    @staticmethod
    def _ok(resp: httpx.Response) -> dict[str, Any]:
        """Return the JSON body on a 2xx, else raise :class:`EmailApiError`.

        Resend uses the HTTP status to signal failure (not a ``200 + ok:false``
        quirk), so any non-2xx is an error. The error ``message`` is read from
        the body's ``message`` / ``error`` field when present.
        """
        if not 200 <= resp.status_code < 300:
            message = "unknown_error"
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("error") or message)
            raise EmailApiError(resp.status_code, message)
        body_ok: dict[str, Any] = resp.json()
        return body_ok

    async def send_email(
        self,
        *,
        sender: str,
        to: str | list[str],
        subject: str,
        html: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Send a single transactional email.

        Exactly one of ``html`` / ``text`` should be supplied; when both are
        omitted an empty ``text`` body is sent so the API call stays valid.
        Returns the Resend response body (``{"id": ...}`` on success).
        """
        payload: dict[str, Any] = {"from": sender, "to": to, "subject": subject}
        if html is not None:
            payload["html"] = html
        if text is not None:
            payload["text"] = text
        if html is None and text is None:
            payload["text"] = ""
        resp = await self._post("/emails", payload)
        return self._ok(resp)


__all__ = ["DEFAULT_BASE_URL", "EmailApiError", "ResendClient"]
