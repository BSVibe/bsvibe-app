"""Thin async Linear GraphQL client built on httpx.

Deliberately small — only the calls the linear plugin needs (create / archive
an issue). The official ``@linear/sdk`` is intentionally NOT used (keeps the
dependency surface to httpx, already a project dep), mirroring
:mod:`plugin.github.client` and
:mod:`plugin.notion.client`.

The client either borrows an injected :class:`httpx.AsyncClient` (preferred
when a caller pools connections) or opens a short-lived one per request.
Tests mock httpx at the transport layer (respx), so no real network I/O.

Two Linear-specific quirks the wrapper handles:

* **Auth header** — a Linear *personal API key* is sent **raw** in the
  ``Authorization`` header (``Authorization: lin_api_...``), NOT prefixed with
  ``Bearer``. (OAuth access tokens *are* ``Bearer`` prefixed, but this plugin
  only supports the simple personal-API-key flow.)
* **GraphQL error envelope** — the GraphQL endpoint can return HTTP 200 while
  the body carries ``{"errors": [...]}`` (query/mutation-level errors). A 200
  is therefore NOT automatically a success: the wrapper inspects the body for
  ``errors`` and raises :class:`LinearApiError`, in addition to raising on any
  non-2xx HTTP status.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.linear.app"
_GRAPHQL_PATH = "/graphql"

_ISSUE_FIELDS = "issue { id identifier url }"

_ISSUE_CREATE_MUTATION = (
    "mutation IssueCreate($input: IssueCreateInput!) {"
    " issueCreate(input: $input) { success " + _ISSUE_FIELDS + " } }"
)

_ISSUE_ARCHIVE_MUTATION = (
    "mutation IssueArchive($id: String!) { issueArchive(id: $id) { success } }"
)


class LinearApiError(RuntimeError):
    """Raised when the Linear GraphQL API reports a query/mutation-level error.

    Carries the raw ``errors`` list (when present) so callers can inspect the
    error code — e.g. to treat an already-archived / not-found issue as a
    no-op during compensation.
    """

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, Any]] = errors or []


class LinearClient:
    """Authenticated wrapper over the Linear GraphQL API."""

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
        # Linear personal API keys are sent RAW in Authorization (no "Bearer ").
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }

    async def _post(self, query: str, variables: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}{_GRAPHQL_PATH}"
        json_body = {"query": query, "variables": variables}
        if self._client is not None:
            return await self._client.request("POST", url, headers=self._headers(), json=json_body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request("POST", url, headers=self._headers(), json=json_body)

    @staticmethod
    def _data(resp: httpx.Response) -> dict[str, Any]:
        """Validate a GraphQL response.

        Raises on non-2xx HTTP status AND on a GraphQL-level ``errors`` envelope
        (a 200 with ``errors`` is a failure, not a success). Returns the
        ``data`` object on success.
        """
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        errors = body.get("errors")
        if errors:
            message = (
                "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))
                or "Linear GraphQL error"
            )
            raise LinearApiError(message, errors=errors)
        data: dict[str, Any] = body.get("data") or {}
        return data

    # ── issues ─────────────────────────────────────────────────────────────────

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create an issue on a team. Returns the created ``issue`` object
        (``{"id", "identifier", "url"}``)."""
        variables = {
            "input": {"teamId": team_id, "title": title, "description": description},
        }
        resp = await self._post(_ISSUE_CREATE_MUTATION, variables)
        data = self._data(resp)
        payload = data.get("issueCreate") or {}
        issue: dict[str, Any] = payload.get("issue") or {}
        if not issue.get("id"):
            raise LinearApiError("Linear issueCreate returned no issue id")
        return issue

    async def archive_issue(self, issue_id: str) -> dict[str, Any]:
        """Archive an issue. Idempotent at the caller layer: an
        already-archived / not-found issue surfaces as a :class:`LinearApiError`
        (with an ``entityNotFound`` code) which the plugin treats as a no-op."""
        resp = await self._post(_ISSUE_ARCHIVE_MUTATION, {"id": issue_id})
        return self._data(resp)


__all__ = ["DEFAULT_BASE_URL", "LinearApiError", "LinearClient"]
