"""Thin async Discord REST API client built on httpx.

Deliberately small — only the calls the discord plugin needs (create / delete a
channel message). No Discord SDK is used (keeps the dependency surface to httpx,
already a project dep).

Discord quirk (differs from slack/telegram): the REST API signals failure with
a **non-2xx HTTP status** plus a JSON error body, rather than HTTP 200 with an
``ok:false`` envelope. :meth:`DiscordClient._json` therefore raises
:class:`DiscordApiError` on any non-2xx response. ``delete_message`` treats a
``404 Not Found`` as an idempotent no-op (the message is already gone) so a
re-delete during compensation is a silent success.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred when
a caller pools connections) or opens a short-lived one per request. Tests mock
httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://discord.com/api/v10"


class DiscordApiError(RuntimeError):
    """Raised when the Discord REST API returns a non-2xx status."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"discord api error {status}: {message}")


class DiscordClient:
    """Authenticated wrapper over the Discord REST API.

    The bot token is sent in the ``Authorization: Bot <token>`` header per the
    Discord convention.
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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            return await self._client.request(method, url, headers=self._headers(), json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, headers=self._headers(), json=json_body)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        """Return the JSON body, raising :class:`DiscordApiError` on non-2xx."""
        if resp.is_success:
            body: dict[str, Any] = resp.json()
            return body
        message = "unknown_error"
        try:
            err = resp.json()
            if isinstance(err, dict):
                message = str(err.get("message", message))
        except ValueError:
            message = resp.text or message
        raise DiscordApiError(resp.status_code, message)

    # ── messages ───────────────────────────────────────────────────────────

    async def create_message(
        self,
        channel_id: str,
        content: str,
        *,
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Post a text message to a channel. Returns the created Message object.

        ``components`` (action rows with buttons) are included ONLY when set — a
        content-only notification sends no ``components`` key."""
        body: dict[str, Any] = {"content": content}
        if components is not None:
            body["components"] = components
        resp = await self._request("POST", f"/channels/{channel_id}/messages", json_body=body)
        return self._json(resp)

    # ── interaction webhook (follow-ups + @original edit) ─────────────────────
    #
    # Interaction responses use the INTERACTION WEBHOOK — ``application_id`` +
    # ``interaction_token`` in the URL — which is valid ~15 min and self-authenticated
    # by the token (no bot token needed). We reach it on the same host, so the bot
    # ``Authorization`` header rides along harmlessly.

    async def create_interaction_followup(
        self,
        application_id: str,
        interaction_token: str,
        content: str,
        *,
        flags: int | None = None,
    ) -> dict[str, Any]:
        """POST a follow-up message to an interaction (``flags=64`` → EPHEMERAL, only
        the tapper sees it). This is how an unauthorized tapper is told "권한이 없어요"
        without touching the public card. Returns the created message object."""
        body: dict[str, Any] = {"content": content}
        if flags is not None:
            body["flags"] = flags
        resp = await self._request(
            "POST", f"/webhooks/{application_id}/{interaction_token}", json_body=body
        )
        return self._json(resp)

    async def edit_interaction_response(
        self,
        application_id: str,
        interaction_token: str,
        content: str,
        *,
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """PATCH the ORIGINAL interaction response (``@original``) — used to close the
        approval loop: keep the card body, append the result line, and drop the
        buttons by passing ``components=[]``. Returns the edited message object."""
        body: dict[str, Any] = {"content": content}
        if components is not None:
            body["components"] = components
        resp = await self._request(
            "PATCH",
            f"/webhooks/{application_id}/{interaction_token}/messages/@original",
            json_body=body,
        )
        return self._json(resp)

    async def delete_message(self, channel_id: str, message_id: str) -> int | None:
        """Delete a message. Returns ``None`` on success, or the HTTP status code
        when the message is already gone (``404``) so the caller can treat a
        re-delete as an idempotent no-op. Any other non-2xx raises
        :class:`DiscordApiError`."""
        resp = await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        if resp.is_success:
            return None
        if resp.status_code == 404:
            return resp.status_code
        message = "unknown_error"
        try:
            err = resp.json()
            if isinstance(err, dict):
                message = str(err.get("message", message))
        except ValueError:
            message = resp.text or message
        raise DiscordApiError(resp.status_code, message)


__all__ = ["DEFAULT_BASE_URL", "DiscordApiError", "DiscordClient"]
