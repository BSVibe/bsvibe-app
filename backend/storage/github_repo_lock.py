"""PR2 — per-repo advisory merge lock (opt-in ``github_auto_merge_enabled``).

Serializes CI-green auto-merge attempts on the *same* GitHub repository
so a later ``MergeWatchWorker`` can never squash-merge two PRs targeting
one ``owner/name`` concurrently (a race that produces spurious merge
conflicts + wasted CI). No caller yet — this is the serialization
primitive the auto-merge worker will wrap around each merge.

It mirrors :func:`backend.storage.product_workspace.product_workspace_lock`
exactly, reusing the same Postgres ``pg_try_advisory_lock`` primitive from
:mod:`backend.workflow.infrastructure.advisory_lock`. The only difference
is the lock *key*: instead of a ``product_id``, the key is derived from
the ``"owner/name"`` repo string via a UUID5 into a fixed private
namespace. Because UUID5 lands in a distinct namespace (and yields a
version-5 UUID), a repo key can never collide with a real product_id
(random version-4 UUIDs) — the two lock spaces are disjoint.

On SQLite (unit tests) the underlying helper falls back to a
process-local ``asyncio.Lock`` keyed by the same UUID, preserving the
serialization contract within a single test process.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.advisory_lock import (
    release_run_dispatch_lock,
    try_run_dispatch_lock,
)

# Arbitrary fixed namespace UUID. Its only job is to make repo->key
# deterministic across processes while guaranteeing (via UUID5's namespace
# separation) that no repo key ever equals a real product_id. Generated once
# and hardcoded — do NOT regenerate; changing it would invalidate every
# in-flight lock key across a deploy.
_REPO_LOCK_NS = uuid.UUID("0765c176-3d01-4a9d-ba51-b9bdb6352e6a")


class GithubRepoBusy(RuntimeError):
    """The per-repo merge lock is held by another session.

    Raised by :func:`github_repo_lock` on the loser path. Its own type
    (NOT :class:`~backend.storage.product_workspace.ProductWorkspaceBusy`)
    — a different domain (GitHub repo serialization vs product workspace
    ship serialization). The caller (the merge worker tick) is expected to
    retry on the next pass rather than block.
    """


def repo_lock_key(repo: str) -> uuid.UUID:
    """Derive a stable lock key from a ``"owner/name"`` repo string.

    Normalizes by stripping surrounding whitespace and lowercasing, so
    ``Owner/Repo`` and ``owner/repo`` map to the *same* lock (GitHub
    owner/repo slugs are case-insensitive). The result is a UUID5 in the
    private :data:`_REPO_LOCK_NS` namespace — deterministic across
    processes and disjoint from the product_id space.
    """
    normalized = repo.strip().lower()
    return uuid.uuid5(_REPO_LOCK_NS, normalized)


@asynccontextmanager
async def github_repo_lock(session: AsyncSession, repo: str) -> AsyncIterator[None]:
    """Serialize CI-green auto-merge attempts on a single GitHub repo.

    Mirrors :func:`product_workspace_lock`: acquire the advisory lock for
    :func:`repo_lock_key`; on the loser path raise :class:`GithubRepoBusy`;
    always release in ``finally``. When two sessions race for the same
    repo, the loser raises and the caller retries on the next worker tick.
    """
    key = repo_lock_key(repo)
    acquired = await try_run_dispatch_lock(session, key)
    if not acquired:
        raise GithubRepoBusy(f"github repo {repo} is busy with another merge")
    try:
        yield
    finally:
        await release_run_dispatch_lock(session, key)


__all__ = [
    "GithubRepoBusy",
    "github_repo_lock",
    "repo_lock_key",
]
