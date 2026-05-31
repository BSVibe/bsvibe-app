"""Thin async Trello REST client built on httpx.

Deliberately small — only the calls the trello plugin needs (create / archive
a card). No SDK: the dependency surface stays at httpx (already a project dep),
mirroring :mod:`backend.extensions.implementations.github.client`,
:mod:`backend.extensions.implementations.notion.client` and
:mod:`backend.extensions.implementations.linear.client`.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.

Trello-specific quirk the wrapper handles:

* **Auth scheme** — Trello does NOT use a ``Bearer`` Authorization header. The
  API key and token are passed as **query parameters** on every request
  (``?key=<api_key>&token=<token>``). This wrapper appends them to the params
  of each call; callers never construct the auth themselves.
* **Failure signalling** — Trello reports failure via the HTTP status (a
  non-2xx response). Unlike GraphQL APIs there is no in-body error envelope on
  a 200, so :meth:`_json` raises :class:`TrelloApiError` on any non-2xx status.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.trello.com"
_CARDS_PATH = "/1/cards"


class TrelloApiError(RuntimeError):
    """Raised when the Trello REST API returns a non-2xx HTTP status.

    Carries the HTTP ``status_code`` so callers can treat an already-gone card
    (404) as a no-op during compensation (Workflow §9).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TrelloClient:
    """Authenticated wrapper over the Trello REST API."""

    def __init__(
        self,
        api_key: str,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _auth_params(self) -> dict[str, str]:
        # Trello auth is query-param based — NOT a Bearer Authorization header.
        return {"key": self._api_key, "token": self._token}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        merged: dict[str, Any] = {**(params or {}), **self._auth_params()}
        if self._client is not None:
            return await self._client.request(method, url, params=merged)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, params=merged)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        """Validate and parse a Trello response.

        Raises :class:`TrelloApiError` on any non-2xx HTTP status (Trello
        signals failure via the status code; a 2xx is always a success)."""
        if not resp.is_success:
            raise TrelloApiError(
                f"Trello API error {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )
        body: dict[str, Any] = resp.json()
        return body

    # ── cards ──────────────────────────────────────────────────────────────────

    async def create_card(
        self,
        *,
        list_id: str,
        name: str,
        desc: str = "",
    ) -> dict[str, Any]:
        """Create a card on a list. Returns the created card object
        (carries ``id`` / ``url`` / ``shortUrl``)."""
        params = {"idList": list_id, "name": name, "desc": desc}
        resp = await self._request("POST", _CARDS_PATH, params=params)
        card = self._json(resp)
        if not card.get("id"):
            raise TrelloApiError("Trello create card returned no id", status_code=resp.status_code)
        return card

    async def archive_card(self, card_id: str) -> int:
        """Archive (close) a card. Returns the HTTP status code; does NOT raise
        on 404 so the caller can treat an already-gone card as a no-op."""
        resp = await self._request("PUT", f"{_CARDS_PATH}/{card_id}", params={"closed": "true"})
        if resp.status_code == 404:
            return resp.status_code
        if not resp.is_success:
            raise TrelloApiError(
                f"Trello archive error {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )
        return resp.status_code


__all__ = ["DEFAULT_BASE_URL", "TrelloApiError", "TrelloClient"]
