"""Thin async GitHub REST client built on httpx.

Deliberately small — only the calls the github plugin needs (open / update /
get / close PR, post / delete issue comment). PyGithub is intentionally NOT
used (keeps the dependency surface to httpx, already a project dep).

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"


class GithubClient:
    """Authenticated wrapper over the GitHub REST API."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
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

    # ── pull requests ──────────────────────────────────────────────────────

    async def open_pr(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str = ""
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_body={"head": head, "base": base, "title": title, "body": body},
        )
        return self._json(resp)

    async def update_pr(self, owner: str, repo: str, number: int, **fields: Any) -> dict[str, Any]:
        payload = {k: v for k, v in fields.items() if v is not None}
        resp = await self._request(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json_body=payload
        )
        return self._json(resp)

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        resp = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return self._json(resp)

    async def close_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        resp = await self._request(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json_body={"state": "closed"}
        )
        return self._json(resp)

    # ── issue comments ───────────────────────────────────────────────────────

    async def post_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json_body={"body": body},
        )
        return self._json(resp)

    async def delete_comment(self, owner: str, repo: str, comment_id: int) -> int:
        """Delete a comment. Returns the HTTP status code; does NOT raise on
        404 so the caller can treat an already-deleted comment as a no-op."""
        resp = await self._request("DELETE", f"/repos/{owner}/{repo}/issues/comments/{comment_id}")
        if resp.status_code not in (204, 404):
            resp.raise_for_status()
        return resp.status_code

    # ── issues (read) ────────────────────────────────────────────────────────

    async def list_issues(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        per_page: int = 20,
    ) -> list[dict[str, Any]]:
        """List issues in a repo. Read-only — exposed as the ``github__list_issues``
        agent-loop action (M2).

        Mirrors ``GET /repos/{owner}/{repo}/issues?state={state}``. Returns the
        raw JSON list so the caller (the action) can shape it. The REST endpoint
        includes pull requests by default — callers filter as needed.
        """
        path = f"/repos/{owner}/{repo}/issues?state={state}&per_page={per_page}"
        resp = await self._request("GET", path)
        resp.raise_for_status()
        body: Any = resp.json()
        if not isinstance(body, list):
            return []
        return body


__all__ = ["DEFAULT_BASE_URL", "GithubClient"]
