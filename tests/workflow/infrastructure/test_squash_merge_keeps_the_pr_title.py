"""#1143 — the merge watch's squash commit on main carries the PR's title.

Measured 2026-10-07: PR #1142 was titled *"라우팅 계정 누락 시 런 멈춤 수정"*, but
the commit the merge watch's auto squash-merge left on main read
``work: (재시도 — 이전 런 4414bcd5 은 …) (run-b2ebd3c4) (#1142)`` — the run's
commit message, which is the directive's first line. The merge request sent only
``{"merge_method": "squash"}``, and for a one-commit PR GitHub's default squash
title IS that commit's message. ``commit_title`` names it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from plugin.github.client import GithubClient, MergeResult
from tests._support import db_engine
from tests.workflow.infrastructure.test_merge_watch_worker import (
    _FakeClient,
    _row,
    _seed,
    _worker,
)

pytestmark = pytest.mark.asyncio


class _TitleRecordingClient(_FakeClient):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.merge_kwargs: list[dict[str, Any]] = []

    async def merge_pr(  # type: ignore[override]
        self, owner: str, repo: str, number: int, **kwargs: Any
    ) -> MergeResult:
        self.merge_kwargs.append(kwargs)
        return await super().merge_pr(owner, repo, number, **kwargs)


async def test_the_squash_commit_is_titled_after_the_pr() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed(sf, _row(pr_number=1142))
        client = _TitleRecordingClient(
            pr={
                "state": "open",
                "merged": False,
                "mergeable_state": "clean",
                "title": "라우팅 계정 누락 시 런 멈춤 수정",
            }
        )

        await _worker(sf, client).drain_once()

    assert client.merge_kwargs[0]["commit_title"] == "라우팅 계정 누락 시 런 멈춤 수정 (#1142)"


async def test_a_pr_without_a_title_leaves_githubs_default() -> None:
    """Control — never invent a title; ``None`` lets GitHub choose as before."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        await _seed(sf, _row())
        client = _TitleRecordingClient(
            pr={"state": "open", "merged": False, "mergeable_state": "clean"}
        )

        await _worker(sf, client).drain_once()

    assert client.merge_kwargs[0].get("commit_title") is None


async def test_the_client_sends_the_commit_title_to_github() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"sha": "abc", "merged": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GithubClient(token="t", base_url="https://api.github.test", client=http)

    await client.merge_pr("o", "r", 7, method="squash", commit_title="Fix it (#7)")

    assert sent == [{"merge_method": "squash", "commit_title": "Fix it (#7)"}]


async def test_without_a_title_the_body_is_unchanged() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"sha": "abc", "merged": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = GithubClient(token="t", base_url="https://api.github.test", client=http)

    await client.merge_pr("o", "r", 7)

    assert sent == [{"merge_method": "squash"}]
