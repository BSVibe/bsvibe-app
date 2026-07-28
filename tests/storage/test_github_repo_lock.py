"""PR2 — per-repo advisory merge lock tests.

Exercises :mod:`backend.storage.github_repo_lock`:

* ``repo_lock_key`` is deterministic and normalizes ``owner/name``
  case + surrounding whitespace, so ``Owner/Repo`` and ``owner/repo``
  map to the *same* lock key; distinct repos map to distinct keys.
* ``github_repo_lock`` serializes concurrent merges on the same repo
  (loser raises :class:`GithubRepoBusy`) while distinct repos do not
  contend.

Mirrors the ``product_workspace_lock`` tests — in-memory SQLite is
enough to exercise the ``asyncio.Lock`` fallback path the advisory-lock
helper picks for non-Postgres dialects.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.storage.github_repo_lock import (
    GithubRepoBusy,
    github_repo_lock,
    repo_lock_key,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# repo_lock_key — deterministic + normalized
# ---------------------------------------------------------------------------


async def test_repo_lock_key_is_deterministic() -> None:
    """The same repo string always maps to the same UUID key."""
    assert repo_lock_key("octocat/hello") == repo_lock_key("octocat/hello")


async def test_repo_lock_key_normalizes_case_and_whitespace() -> None:
    """``Owner/Repo`` and ``  owner/repo  `` map to the same lock key."""
    assert repo_lock_key("Owner/Repo") == repo_lock_key("owner/repo")
    assert repo_lock_key("  Owner/Repo  ") == repo_lock_key("owner/repo")


async def test_repo_lock_key_distinct_repos_differ() -> None:
    """Different repos map to different keys."""
    assert repo_lock_key("octocat/hello") != repo_lock_key("octocat/world")


async def test_repo_lock_key_disjoint_from_product_id_space() -> None:
    """A repo key derived via uuid5 into the private namespace is version 5,
    so it can never collide with a random (version 4) product_id."""
    assert repo_lock_key("octocat/hello").version == 5


# ---------------------------------------------------------------------------
# github_repo_lock — serialization
# ---------------------------------------------------------------------------


async def test_github_repo_lock_busy_raises() -> None:
    """A second acquire on the same repo (from a different task) sees
    GithubRepoBusy. The caller (the merge worker tick) retries on the
    next pass — the lock is not blocking by design."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    repo = "octocat/hello"

    async with sessionmaker() as s1, sessionmaker() as s2:
        async with github_repo_lock(s1, repo):

            async def _try_other_task() -> None:
                with pytest.raises(GithubRepoBusy):
                    async with github_repo_lock(s2, repo):
                        pass

            await asyncio.create_task(_try_other_task())

    await engine.dispose()


async def test_github_repo_lock_same_repo_different_case_contends() -> None:
    """``Owner/Repo`` and ``owner/repo`` resolve to the same key, so the
    second acquire is busy even though the string differs by case."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as s1, sessionmaker() as s2:
        async with github_repo_lock(s1, "Owner/Repo"):

            async def _try_other_task() -> None:
                with pytest.raises(GithubRepoBusy):
                    async with github_repo_lock(s2, "owner/repo"):
                        pass

            await asyncio.create_task(_try_other_task())

    await engine.dispose()


async def test_github_repo_lock_distinct_repos_do_not_contend() -> None:
    """Two different repos acquire concurrently without contention."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as s1, sessionmaker() as s2:
        async with github_repo_lock(s1, "octocat/hello"):
            # Different repo — must NOT be busy.
            async def _other_repo() -> None:
                async with github_repo_lock(s2, "octocat/world"):
                    pass

            await asyncio.create_task(_other_repo())

    await engine.dispose()


async def test_github_repo_lock_releases_on_exit() -> None:
    """After the context manager exits, a fresh acquire succeeds."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    repo = "octocat/hello"

    async with sessionmaker() as s:
        async with github_repo_lock(s, repo):
            pass

        async with github_repo_lock(s, repo):
            pass

    await engine.dispose()


async def test_github_repo_busy_is_its_own_type() -> None:
    """GithubRepoBusy is a distinct RuntimeError, NOT ProductWorkspaceBusy."""
    from backend.storage.product_workspace import ProductWorkspaceBusy

    assert issubclass(GithubRepoBusy, RuntimeError)
    assert not issubclass(GithubRepoBusy, ProductWorkspaceBusy)
