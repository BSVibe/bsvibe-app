"""Thin async Telegram Bot API client built on httpx.

Deliberately small — only the calls the telegram plugin needs (send / delete a
message). No Telegram SDK is used (keeps the dependency surface to httpx,
already a project dep).

Telegram quirk (mirrors slack): the Bot API returns HTTP 200 even for *logical*
failures, with a ``{"ok": false, "description": "..."}`` body.
:meth:`TelegramClient._ok` therefore checks ``ok`` after ``raise_for_status``
and raises :class:`TelegramApiError` rather than treating any 200 as success.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.telegram.org"


class TelegramApiError(RuntimeError):
    """Raised when the Telegram Bot API returns ``{"ok": false, "description": ...}``."""

    def __init__(self, description: str) -> None:
        self.description = description
        super().__init__(f"telegram api error: {description}")


class TelegramClient:
    """Authenticated wrapper over the Telegram Bot API.

    The bot token is embedded in the URL path (``/bot<token>/<method>``) per the
    Bot API convention, so it never appears in a header or query string.
    """

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

    async def _post(self, method: str, json_body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}/bot{self._token}/{method}"
        if self._client is not None:
            return await self._client.post(url, json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, json=json_body)

    @staticmethod
    def _ok(resp: httpx.Response) -> dict[str, Any]:
        """Raise on transport error, then on Telegram's ``ok:false`` body."""
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if not body.get("ok", False):
            raise TelegramApiError(str(body.get("description", "unknown_error")))
        return body

    # ── messages ───────────────────────────────────────────────────────────

    async def send_message(self, chat_id: str | int, text: str) -> dict[str, Any]:
        """Send a text message. Returns the ``result`` object (the sent
        ``Message``) from a successful response."""
        resp = await self._post("sendMessage", {"chat_id": chat_id, "text": text})
        body = self._ok(resp)
        result: dict[str, Any] = body["result"]
        return result

    async def delete_message(self, chat_id: str | int, message_id: int) -> str | None:
        """Delete a message. Returns ``None`` on success, or the Telegram error
        description when the message is already gone ("message to delete not
        found") so the caller can treat a re-delete as an idempotent no-op. Any
        other ``ok:false`` error raises :class:`TelegramApiError`."""
        resp = await self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if body.get("ok", False):
            return None
        description = str(body.get("description", "unknown_error"))
        if "message to delete not found" in description.lower():
            return description
        raise TelegramApiError(description)


__all__ = ["DEFAULT_BASE_URL", "TelegramApiError", "TelegramClient"]
