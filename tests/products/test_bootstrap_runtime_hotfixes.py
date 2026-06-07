"""Lift E8 Bug 1 + Bug 2 — bootstrap runtime hot-fix tests.

Two regressions surfaced together by the qazasa123 dogfood:

* **Bug 1** — ``_resolve_via_caller`` was being called WITHOUT a redis client
  from inside ``build_bootstrap_knowledge._ingest_callable``. An executor
  account returned by the resolver then raised ``ExecutorAdapterUnavailable``
  on its first chat call, the IngestCompiler chunk loop caught and counted
  the failure, and every chunk silently dropped.
* **Bug 2** — the runtime marked ``bootstrap_status="complete"`` based on
  ``artifacts_count`` alone, even when zero notes were written. The dogfood
  saw ``bootstrap_artifacts_count=1377`` / 0 notes / status ``complete`` —
  the founder UI said "all good" with an empty knowledge graph.

The fix passes redis through ``build_bootstrap_knowledge`` ->
``_resolve_via_caller`` and decides terminal status from the ingest's
``notes_written`` + ``chunk_failures`` signal instead of from artifacts_count.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.identity.workspaces_db import ProductRow, WorkspaceRow, WorkspacesBase
from backend.workflow.application.runtime.product_bootstrap_runtime import (
    STATUS_COMPLETE,
    STATUS_FAILED_INGEST,
    run_product_bootstrap_job,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


@pytest_asyncio.fixture
async def session_factory():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_and_product(
    session_factory, *, workspace_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    async with session_factory() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region=_REGION, safe_mode=False))
        await s.flush()
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=workspace_id,
                name="p",
                slug="p",
                repo_url="https://x/y",
            )
        )
        await s.commit()


def _stub_settings(tmp_path: Path) -> None:
    from backend.config import get_settings

    settings = get_settings()
    product_root = tmp_path / "product_ws"
    product_root.mkdir(exist_ok=True)
    vault_root = tmp_path / "vault"
    vault_root.mkdir(exist_ok=True)
    object.__setattr__(settings, "product_workspace_root", str(product_root))
    object.__setattr__(settings, "knowledge_vault_root", str(vault_root))


# ── Bug 2 ─────────────────────────────────────────────────────────────────────


async def test_bootstrap_marks_failed_when_every_chunk_dropped(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 2 — when ingest produces zero notes BUT chunk_failures > 0 the
    runtime MUST mark ``failed:ingest`` with a descriptive error, never
    ``complete``.

    Pre-fix behaviour: outcome.artifacts_count > 0 was sufficient to flip
    the row to ``complete`` even when no notes were written.
    """
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )
    _stub_settings(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    class _AllChunksFailKnowledge:
        async def ingest(self, request):
            from backend.knowledge.facade import IngestResult

            # Mimic the qazasa123 dogfood symptom: every chunk dropped.
            return IngestResult(
                proposals_count=0,
                notes_count=0,
                run_id=uuid.uuid4(),
                notes_created=0,
                notes_updated=0,
                chunk_failures=7,
            )

        async def retrieve_canon(self, query):
            from backend.knowledge.facade import CanonRetrievalResult

            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id, region):
            return 0

    monkeypatch.setattr(
        "backend.workflow.application.runtime.product_bootstrap_runtime.build_bootstrap_knowledge",
        lambda **_kw: _AllChunksFailKnowledge(),
    )

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_FAILED_INGEST
        assert row.bootstrap_error is not None
        assert "ingest failed" in row.bootstrap_error
        assert "7" in row.bootstrap_error  # the chunk_failures count


async def test_bootstrap_marks_complete_when_some_notes_written_despite_failures(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 2 corollary — partial success still ships. As long as at least one
    chunk wrote a note, the bootstrap is ``complete`` (with the founder UI
    showing the row), even if some chunks dropped along the way.
    """
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )
    _stub_settings(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    class _PartialKnowledge:
        async def ingest(self, request):
            from backend.knowledge.facade import IngestResult

            return IngestResult(
                proposals_count=0,
                notes_count=3,
                run_id=uuid.uuid4(),
                notes_created=3,
                notes_updated=0,
                chunk_failures=1,
            )

        async def retrieve_canon(self, query):
            from backend.knowledge.facade import CanonRetrievalResult

            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id, region):
            return 0

    monkeypatch.setattr(
        "backend.workflow.application.runtime.product_bootstrap_runtime.build_bootstrap_knowledge",
        lambda **_kw: _PartialKnowledge(),
    )

    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
    )

    async with session_factory() as s:
        row = await s.get(ProductRow, product_id)
        assert row is not None
        assert row.bootstrap_status == STATUS_COMPLETE


# ── Bug 1 ─────────────────────────────────────────────────────────────────────


async def test_bootstrap_runtime_threads_redis_into_resolver(
    session_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 1 — ``_ingest_callable`` MUST forward a redis client to
    ``_resolve_via_caller`` so an executor adapter can dispatch onto the
    worker stream. Pre-fix, the closure called the resolver with the default
    ``redis=None`` and every chunk silently dropped.

    The test spies on ``_resolve_via_caller`` from inside the runtime module
    and asserts the ``redis`` kwarg the bootstrap path supplies is the SAME
    redis client the caller passed into ``run_product_bootstrap_job``.
    """
    from backend.workflow.application.runtime import product_bootstrap_runtime as runtime_mod

    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await _seed_workspace_and_product(
        session_factory, workspace_id=workspace_id, product_id=product_id
    )
    _stub_settings(tmp_path)

    fake_git = MagicMock()
    fake_git.clone = AsyncMock()

    # A sentinel object stand-in for the redis client. The test never calls
    # any redis verbs on it — it only verifies the resolver received it.
    sentinel_redis = object()

    spy: dict[str, object] = {}

    real_resolver = runtime_mod._resolve_via_caller

    async def _spy_resolver(*args, **kwargs):
        # Capture the FIRST call (the bootstrap _ingest_callable path).
        # Any later callers (e.g. the agent runtime) would over-write, but
        # in this test only the bootstrap path runs.
        spy.setdefault("redis", kwargs.get("redis"))
        spy.setdefault("caller_id", kwargs.get("caller_id"))
        return await real_resolver(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "_resolve_via_caller", _spy_resolver)

    # Use the real ``build_bootstrap_knowledge`` so the _ingest_callable
    # actually runs and triggers the resolver call we are spying on. The
    # resolver returns None for this empty test workspace, so ingest
    # short-circuits to notes=0, chunk_failures=0 -> the runtime marks
    # ``complete`` (no chunks ran => no failures) per Bug 2 policy. That's
    # fine for Bug 1's purposes — we only need to observe the resolver call.
    await run_product_bootstrap_job(
        product_id=product_id,
        workspace_id=workspace_id,
        repo_url="https://x/y",
        session_factory=session_factory,
        git_ops=fake_git,
        redis_client=sentinel_redis,
    )

    assert spy.get("caller_id") == "knowledge.ingest", (
        f"expected the bootstrap path to call the resolver for caller_id "
        f"'knowledge.ingest', got: {spy.get('caller_id')!r}"
    )
    assert spy.get("redis") is sentinel_redis, (
        "Bug 1 regressed: ``_resolve_via_caller`` got redis=None instead of "
        "the client the runtime was supplied with. Without it, an executor "
        "adapter will raise ExecutorAdapterUnavailable on every chunk."
    )
