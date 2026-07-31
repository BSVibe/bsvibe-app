"""``GithubClient.open_pr`` idempotency — conflict-resolution re-deliver safety.

When a run's github deliverable is re-delivered (e.g. after a merge-conflict
re-drive pushes the resolution to the SAME branch), the delivery path calls
``open_pr`` again for a head branch that already has an open PR. GitHub answers
``POST /pulls`` with 422 "A pull request already exists". The git push already
landed the resolution, so ``open_pr`` must be idempotent: on that 422, look up
the existing open PR for the head and return it instead of raising.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from plugin.github.client import GithubClient

pytestmark = pytest.mark.asyncio

_EXISTING_PR = {"number": 2, "html_url": "https://github.com/o/r/pull/2", "state": "open"}


def _client(handler) -> GithubClient:
    transport = httpx.MockTransport(handler)
    return GithubClient("tok", client=httpx.AsyncClient(transport=transport))


async def test_open_pr_creates_when_none_exists() -> None:
    calls: list[tuple[str, str]] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.method == "POST"
        return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"})

    pr = await _client(_handler).open_pr("o", "r", head="feat/x", base="main", title="t")
    assert pr["number"] == 7
    # Common path: exactly one call (the create), no extra lookup.
    assert calls == [("POST", "/repos/o/r/pulls")]


async def test_open_pr_reuses_existing_on_422() -> None:
    calls: list[tuple[str, str]] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for o:feat/x."}],
                },
            )
        # GET /pulls?head=o:feat/x&state=open
        assert req.method == "GET"
        assert req.url.params.get("head") == "o:feat/x"
        assert req.url.params.get("state") == "open"
        return httpx.Response(200, json=[_EXISTING_PR])

    pr = await _client(_handler).open_pr("o", "r", head="feat/x", base="main", title="t")
    # Idempotent: returns the existing PR, does not raise.
    assert pr["number"] == 2
    assert [m for m, _ in calls] == ["POST", "GET"]


async def test_open_pr_reraises_422_when_no_existing_pr() -> None:
    def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed", "errors": []})
        return httpx.Response(200, json=[])  # no open PR for the head

    with pytest.raises(httpx.HTTPStatusError):
        await _client(_handler).open_pr("o", "r", head="feat/x", base="main", title="t")


async def test_open_pr_head_filter_uses_owner_qualified_branch() -> None:
    seen: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(422, json={"errors": [{"message": "already exists"}]})
        seen["head"] = req.url.params.get("head")
        return httpx.Response(200, json=[_EXISTING_PR])

    # A slash-bearing branch name must still be filtered as owner:branch.
    await _client(_handler).open_pr("o", "r", head="bsvibe/run-abc", base="main", title="t")
    assert seen["head"] == "o:bsvibe/run-abc"
