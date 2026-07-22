"""Thin async Slack Web API client built on httpx.

Deliberately small — only the calls the slack plugin needs (post / update /
delete a chat message). No Slack SDK is used (keeps the dependency surface to
httpx, already a project dep).

Slack quirk: the Web API returns HTTP 200 even for *logical* failures, with a
``{"ok": false, "error": "..."}`` body. :meth:`SlackClient._ok` therefore
checks ``ok`` after ``raise_for_status`` and raises :class:`SlackApiError`
rather than treating any 200 as success.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://slack.com/api"


class SlackApiError(RuntimeError):
    """Raised when the Slack Web API returns ``{"ok": false, "error": ...}``."""

    def __init__(self, error: str) -> None:
        self.error = error
        super().__init__(f"slack api error: {error}")


class SlackClient:
    """Authenticated wrapper over the Slack Web API."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = bot_token
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _post(self, method: str, json_body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}/{method}"
        if self._client is not None:
            return await self._client.post(url, headers=self._headers(), json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, headers=self._headers(), json=json_body)

    @staticmethod
    def _ok(resp: httpx.Response) -> dict[str, Any]:
        """Raise on transport error, then on Slack's ``ok:false`` body."""
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if not body.get("ok", False):
            raise SlackApiError(str(body.get("error", "unknown_error")))
        return body

    # ── chat ──────────────────────────────────────────────────────────────

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        # ``blocks`` render the rich card; ``text`` is kept as the required
        # accessibility / notification fallback (Slack's guidance for block msgs).
        if blocks is not None:
            payload["blocks"] = blocks
        resp = await self._post("chat.postMessage", payload)
        return self._ok(resp)

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            payload["blocks"] = blocks
        resp = await self._post("chat.update", payload)
        return self._ok(resp)

    async def respond(
        self, response_url: str, text: str, *, response_type: str = "ephemeral"
    ) -> None:
        """POST an ephemeral (default) reply to an interactivity ``response_url``.

        Slack's ``response_url`` is a pre-signed, self-authenticated endpoint (no
        bearer token needed); ``response_type='ephemeral'`` + ``replace_original:
        false`` shows a private note to the tapper without touching the shared
        card. Best-effort — the caller treats a failure as cosmetic."""
        body = {"text": text, "response_type": response_type, "replace_original": False}
        if self._client is not None:
            await self._client.post(response_url, json=body)
            return
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            await client.post(response_url, json=body)

    async def delete_message(self, channel: str, ts: str) -> str | None:
        """Delete a message. Returns ``None`` on success, or the Slack error
        code when the message is already gone (``message_not_found``) so the
        caller can treat a re-delete as an idempotent no-op. Any other
        ``ok:false`` error raises :class:`SlackApiError`."""
        resp = await self._post("chat.delete", {"channel": channel, "ts": ts})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if body.get("ok", False):
            return None
        error = str(body.get("error", "unknown_error"))
        if error == "message_not_found":
            return error
        raise SlackApiError(error)


__all__ = ["DEFAULT_BASE_URL", "SlackApiError", "SlackClient"]
