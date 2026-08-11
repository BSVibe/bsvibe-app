"""Thin async GitHub REST client built on httpx.

Deliberately small — only the calls the github plugin needs (open / update /
get / close PR, post / delete issue comment). PyGithub is intentionally NOT
used (keeps the dependency surface to httpx, already a project dep).

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a PR merge attempt.

    ``merged`` mirrors ``status == "merged"`` for callers that only want the
    boolean. ``not_mergeable`` (405) and ``head_changed`` (409) are NON-error
    outcomes — the caller (a later CI-green merge worker) decides whether to
    wait/re-poll or re-read the head — so ``merge_pr`` returns them instead of
    raising.
    """

    status: Literal["merged", "not_mergeable", "head_changed"]
    merged: bool
    sha: str | None = None


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
        """Open a PR — idempotent on the head branch.

        A re-delivery (e.g. after a merge-conflict re-drive pushes the resolution
        to the SAME branch) calls this again for a head that already has an open
        PR; GitHub answers ``POST /pulls`` with 422 "A pull request already
        exists". The push already landed, so on that 422 we look up the existing
        open PR for the head and return it instead of raising. A 422 with no
        existing PR (a genuine validation error) still raises via ``_json``.
        """
        resp = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_body={"head": head, "base": base, "title": title, "body": body},
        )
        if resp.status_code == 422:
            existing = await self._find_open_pr_for_head(owner, repo, head)
            if existing is not None:
                return existing
        return self._json(resp)

    async def compare_branch(
        self, owner: str, repo: str, *, base: str, head: str
    ) -> dict[str, Any]:
        """How far ``head`` is ahead of ``base`` — ``{exists, ahead_by}``.

        The remote's answer to "is there anything here to open a PR from". A
        caller that pushed the branch itself may believe it is there when the
        push failed, or that it is absent when an earlier attempt landed it; the
        repo is the only authority. ``exists=False`` for an unknown head (404)
        rather than raising, because "no branch" is a legitimate outcome, not a
        fault.
        """
        ref = f"{quote(base, safe='')}...{quote(head, safe='')}"
        resp = await self._request("GET", f"/repos/{owner}/{repo}/compare/{ref}")
        if resp.status_code == 404:
            return {"exists": False, "ahead_by": 0}
        data = self._json(resp)
        return {"exists": True, "ahead_by": int(data.get("ahead_by") or 0)}

    async def _find_open_pr_for_head(
        self, owner: str, repo: str, head: str
    ) -> dict[str, Any] | None:
        """Return the first OPEN PR whose head is ``head`` (``None`` if none).

        The list-pulls ``head`` filter expects ``owner:branch`` — qualify a bare
        branch name with the repo owner."""
        head_q = head if ":" in head else f"{owner}:{head}"
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls?head={quote(head_q, safe='')}&state=open",
        )
        if resp.status_code != 200:
            return None
        prs = resp.json()
        if isinstance(prs, list) and prs:
            first: dict[str, Any] = prs[0]
            return first
        return None

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

    async def merge_pr(
        self, owner: str, repo: str, number: int, *, method: str = "squash"
    ) -> MergeResult:
        """Merge a PR via ``PUT /repos/{owner}/{repo}/pulls/{number}/merge``.

        Does NOT use ``_json`` (which raises). The two "not yet" outcomes are
        returned as data, not exceptions, because a CI-green merge worker treats
        them as retryable states:

        * ``200`` → :class:`MergeResult` ``merged`` (carries the merge ``sha``).
        * ``405`` (Method Not Allowed — PR not mergeable, or already merged) →
          ``not_mergeable`` (re-poll later).
        * ``409`` (Conflict — head moved since the client last read it) →
          ``head_changed`` (re-read the head, retry).
        * any other non-2xx → raises via ``raise_for_status``.
        """
        resp = await self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json_body={"merge_method": method},
        )
        if resp.status_code == 200:
            body: dict[str, Any] = resp.json()
            return MergeResult(status="merged", merged=True, sha=body.get("sha"))
        if resp.status_code == 405:
            return MergeResult(status="not_mergeable", merged=False)
        if resp.status_code == 409:
            return MergeResult(status="head_changed", merged=False)
        resp.raise_for_status()
        # Unreachable for any documented non-2xx, but keeps mypy's return
        # analysis total: a 2xx other than 200 is unexpected here.
        raise httpx.HTTPStatusError(
            f"github: unexpected merge status {resp.status_code}",
            request=resp.request,
            response=resp,
        )

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        """List check-runs for a commit ``ref``.

        Mirrors ``GET /repos/{owner}/{repo}/commits/{ref}/check-runs``. Returns
        the raw ``check_runs`` list (each entry carries at least ``name``,
        ``status``, ``conclusion``) — the caller (a later PR) interprets the
        conclusions. A failed GET is a real error, so this uses the raising path.
        """
        resp = await self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        runs = body.get("check_runs", [])
        if not isinstance(runs, list):
            return []
        return runs

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


__all__ = ["DEFAULT_BASE_URL", "GithubClient", "MergeResult"]
