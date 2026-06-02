"""Thin async Notion REST client built on httpx.

Deliberately small — only the calls the notion plugin needs (create / get /
archive a page, append blocks to a page). The official ``notion-client`` SDK
is intentionally NOT used (keeps the dependency surface to httpx, already a
project dep), mirroring :mod:`plugin.github.client`.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.notion.com"
# Hardcoded per Lift Q3-Notion spec (gotcha #1). Notion treats this
# header as a contract — bumping it can change response shapes (e.g.
# property schemas), so we pin to a known-good version.
_NOTION_VERSION = "2022-06-28"

# Notion's documented limits: 3 requests/sec/integration. The plugin
# action throttles between paginated calls; the client itself doesn't
# enforce per-request to keep the unit surface pure.
_PAGE_SIZE = 100  # max allowed by /search and database queries


class NotionClient:
    """Authenticated wrapper over the Notion REST API."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        notion_version: str = _NOTION_VERSION,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._notion_version,
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
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    @staticmethod
    def _title_property(title: str) -> dict[str, Any]:
        """Build the Notion ``title`` property payload from a plain string."""
        return {"title": [{"type": "text", "text": {"content": title}}]}

    @staticmethod
    def _paragraph_blocks(body: str) -> list[dict[str, Any]]:
        """Split ``body`` into paragraph blocks (one per non-empty line)."""
        blocks: list[dict[str, Any]] = []
        for line in body.splitlines() or [body]:
            text = line.strip()
            if not text:
                continue
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
                }
            )
        return blocks

    # ── pages ────────────────────────────────────────────────────────────────

    async def create_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create a page under a parent page. ``body`` becomes paragraph blocks."""
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": self._title_property(title)},
        }
        children = self._paragraph_blocks(body)
        if children:
            payload["children"] = children
        resp = await self._request("POST", "/v1/pages", json_body=payload)
        return self._json(resp)

    async def get_page(self, page_id: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/v1/pages/{page_id}")
        return self._json(resp)

    async def archive_page(self, page_id: str) -> int:
        """Archive (trash) a page. Returns the HTTP status code; does NOT raise
        on 404 so the caller can treat an already-gone page as a no-op."""
        resp = await self._request("PATCH", f"/v1/pages/{page_id}", json_body={"archived": True})
        if resp.status_code not in (200, 404):
            resp.raise_for_status()
        return resp.status_code

    # ── blocks ─────────────────────────────────────────────────────────────────

    async def append_block(self, page_id: str, text: str) -> dict[str, Any]:
        """Append a paragraph block to an existing page (or block)."""
        resp = await self._request(
            "PATCH",
            f"/v1/blocks/{page_id}/children",
            json_body={"children": self._paragraph_blocks(text) or self._paragraph_blocks(" ")},
        )
        return self._json(resp)

    # ── knowledge import (Lift Q3-Notion) ──────────────────────────────────

    async def _paginated_post(
        self,
        path: str,
        *,
        base_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk a Notion paginated POST endpoint, yielding each result item.

        Notion paginates via ``has_more`` / ``next_cursor``: when ``has_more``
        is ``True`` the caller must re-POST the same body with
        ``start_cursor`` set to the previous response's ``next_cursor``.
        ``page_size`` is set to the maximum (100) to minimise round-trips.
        """
        cursor: str | None = None
        while True:
            body: dict[str, Any] = dict(base_body or {})
            body["page_size"] = _PAGE_SIZE
            if cursor:
                body["start_cursor"] = cursor
            resp = await self._request("POST", path, json_body=body)
            data = self._json(resp)
            for item in data.get("results") or []:
                yield item
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    async def _paginated_get(
        self,
        path: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk a Notion paginated GET endpoint (``/v1/blocks/{id}/children``).

        Same shape as ``_paginated_post`` but cursor is passed via the
        ``start_cursor`` query string parameter per Notion's REST contract.
        """
        cursor: str | None = None
        while True:
            url = f"{path}?page_size={_PAGE_SIZE}"
            if cursor:
                url = f"{url}&start_cursor={cursor}"
            resp = await self._request("GET", url)
            data = self._json(resp)
            for item in data.get("results") or []:
                yield item
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def search_pages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield every page object the integration has access to.

        Notion's ``/v1/search`` returns *pages + databases mixed*; we
        filter to pages only (spec gotcha #2) so the import action does
        not have to defend against database objects appearing in the
        page stream.
        """
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
        }
        return self._paginated_post("/v1/search", base_body=body)

    def query_database(self, database_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every page in a Notion database.

        Pagination follows the same ``has_more`` / ``next_cursor`` shape
        as ``/search``; we pass no filter so every page is yielded
        (the action selects bindings, not per-page logic).
        """
        return self._paginated_post(f"/v1/databases/{database_id}/query")

    def list_block_children(self, block_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every direct child block of a page or block.

        The caller recurses one level into ``has_children: true`` blocks
        (nested lists, toggles) but stops at ``child_page`` blocks —
        those are links to other pages, not embedded content (gotcha #4).
        """
        return self._paginated_get(f"/v1/blocks/{block_id}/children")


__all__ = ["DEFAULT_BASE_URL", "NotionClient"]
