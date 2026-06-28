"""A connector-inbound trigger (e.g. a github issue) binds to the product whose
``repo_url`` matches the event's repo — so it is processed like a Direct message
ON that product (clone + work in context + repo-native PR) instead of running
unbound in an empty workspace.
"""

from __future__ import annotations

import uuid

from backend.api.webhooks import _product_id_for_repo, _repo_slug
from backend.identity.workspaces_db import ProductRow
from tests._support import memory_session


def test_repo_slug_normalizes_urls_and_slugs() -> None:
    assert _repo_slug("https://github.com/blas1n/bsvibe-gh-e2e") == "blas1n/bsvibe-gh-e2e"
    assert _repo_slug("https://github.com/blas1n/bsvibe-gh-e2e.git") == "blas1n/bsvibe-gh-e2e"
    assert _repo_slug("blas1n/bsvibe-gh-e2e") == "blas1n/bsvibe-gh-e2e"
    assert _repo_slug("git@github.com:blas1n/bsvibe-gh-e2e.git") == "blas1n/bsvibe-gh-e2e"
    assert _repo_slug("HTTPS://GitHub.com/Blas1n/BSVibe-GH-E2E") == "blas1n/bsvibe-gh-e2e"


async def test_resolves_product_by_repo_url() -> None:
    ws = uuid.uuid4()
    pid = uuid.uuid4()
    async with memory_session() as s:
        s.add(
            ProductRow(
                id=pid,
                workspace_id=ws,
                name="X",
                slug="x",
                repo_url="https://github.com/blas1n/bsvibe-gh-e2e",
            )
        )
        await s.commit()
        # the github webhook payload carries the bare ``owner/name``
        assert await _product_id_for_repo(s, ws, "blas1n/bsvibe-gh-e2e") == pid


async def test_no_matching_product_returns_none() -> None:
    ws = uuid.uuid4()
    async with memory_session() as s:
        s.add(
            ProductRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                name="X",
                slug="x",
                repo_url="https://github.com/other/repo",
            )
        )
        await s.commit()
        # never bind to an UNRELATED repo's product
        assert await _product_id_for_repo(s, ws, "blas1n/bsvibe-gh-e2e") is None


async def test_scoped_to_workspace() -> None:
    ws1 = uuid.uuid4()
    ws2 = uuid.uuid4()
    async with memory_session() as s:
        s.add(
            ProductRow(
                id=uuid.uuid4(),
                workspace_id=ws2,
                name="X",
                slug="x",
                repo_url="https://github.com/blas1n/bsvibe-gh-e2e",
            )
        )
        await s.commit()
        assert await _product_id_for_repo(s, ws1, "blas1n/bsvibe-gh-e2e") is None
