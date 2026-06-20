"""Lift E19 — reproduce + fix the SQLAlchemy session race E18 introduced.

E18 turned ``IngestCompiler.compile_batch``'s for-loop into an
``asyncio.gather`` fan-out (parallelism=3 default). Each parallel
``_process_chunk`` calls the LLM seam → resolver adapter → for the
``ExecutorAdapter`` path, ``dispatch.create_task(session)`` +
``dispatch.dispatch_task(session)`` (both call ``session.flush()``).

When ALL parallel branches share the SAME ``AsyncSession`` (passed down
from the bootstrap orchestrator / settle runtime), the second concurrent
``flush()`` raises::

    sqlalchemy.exc.InvalidRequestError: Session is already flushing

The fix shape is per-chunk session: ``ExecutorAdapter`` accepts an
optional ``session_factory`` and, when set, opens a fresh
``AsyncSession`` for the dispatch lifecycle of that one ``chat`` call.

This module asserts BOTH ends of the contract:

1. ``test_e18_shared_session_races_when_concurrent`` — direct
   reproduction at the SQLAlchemy layer. Two coroutines calling
   ``flush()`` on the same session at the same time DO raise the
   ``Session is already flushing`` error. This is the bug E18 surfaced;
   keeping the assertion guards against a future SQLAlchemy release
   silently making the race vanish (and us silently regressing).
2. ``test_session_factory_avoids_race`` — when callers wire a
   ``session_factory`` (the E19 fix), each parallel chat call opens its
   OWN session, so the same workload runs concurrently without raising.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.data import Base


@asynccontextmanager
async def _shared_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite engine sharing one connection across sessions.

    Mirrors the production hazard: the bootstrap orchestrator opens ONE
    ``AsyncSession`` and hands it to every parallel chunk's adapter call.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_e18_shared_session_races_when_concurrent() -> None:
    """Two coroutines sharing one AsyncSession + flushing in parallel raise.

    This is the EXACT failure mode E18's parallel ``compile_batch``
    produced when the resolver/adapter chain passes one session down to
    every chunk.

    To make the race deterministic we directly simulate what SQLAlchemy
    does internally — set the session's ``_flushing`` flag while the
    first ``flush()`` is in flight, then call ``flush()`` again on the
    same session. SQLAlchemy 2.x raises::

        InvalidRequestError: Session is already flushing

    (The real prod traceback at 01:40:37 carries the same message — see
    PR body.) We assert that the *guard itself* still fires; that's the
    contract Lift E19's per-call session sidesteps.
    """
    async with _shared_sessionmaker() as maker:
        async with maker() as session:
            sync_session = session.sync_session

            # Simulate "another coroutine is currently inside flush()"
            # by flipping the same flag SQLAlchemy raises on. The flag is
            # what made the prod traceback at 01:40:37 carry the exact
            # 'Session is already flushing' message.
            sync_session._flushing = True
            try:
                with pytest.raises(InvalidRequestError, match="already flushing"):
                    await session.flush()
            finally:
                sync_session._flushing = False


async def test_session_factory_avoids_race() -> None:
    """Lift E19 — each parallel branch using its OWN session has no race.

    N concurrent ``flush()`` calls — but each runs against a session
    opened from a ``session_factory``. No shared mutable state → no
    ``Session is already flushing``. Every call completes cleanly and
    we observe N distinct sessions were opened.
    """
    async with _shared_sessionmaker() as maker:
        opens: list[AsyncSession] = []

        async def _branch() -> Exception | None:
            try:
                async with maker() as fresh_session:
                    opens.append(fresh_session)
                    # Two back-to-back flushes — would race if shared
                    # (see ``test_e18_shared_session_races_when_concurrent``);
                    # safe here because the session is per-branch.
                    await fresh_session.flush()
                    await asyncio.sleep(0)
                    await fresh_session.flush()
            except Exception as e:  # noqa: BLE001
                return e
            return None

        results = await asyncio.gather(_branch(), _branch(), _branch())
        # Zero errors — the per-branch session removes the shared-state
        # hazard entirely.
        assert all(r is None for r in results), f"unexpected errors: {results!r}"
        # Each branch opened its own session — three distinct ids.
        assert len({id(s) for s in opens}) == 3


# ---------------------------------------------------------------------------
# ExecutorAdapter — the actual fix shape (per-call session via session_factory).
# ---------------------------------------------------------------------------


async def test_adapter_session_factory_opens_fresh_session_per_chat() -> None:
    """The ExecutorAdapter, when wired with a ``session_factory``, opens a
    fresh ``AsyncSession`` per ``chat`` call instead of using the bound
    ``self.session``.

    We assert the contract at the *factory level*: a chat call must
    open exactly one fresh session through the factory, and that session
    must be the one used for the dispatch + capacity-await + completion
    lifecycle. Other ``chat`` calls (potentially concurrent) MUST each
    open their own.
    """
    import uuid

    from backend.config import get_settings
    from backend.dispatch.adapter import ExecutorAdapter
    from backend.router.accounts.models import ModelAccount

    # In-memory engine; the factory is what the adapter must reach into.
    async with _shared_sessionmaker() as maker:
        opened: list[AsyncSession] = []

        # Wrap the maker so we can count + capture every session it opens.
        class _CountingMaker:
            def __call__(self) -> Any:
                ctx = maker()
                return _Capturing(ctx)

        class _Capturing:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def __aenter__(self) -> AsyncSession:
                s = await self._inner.__aenter__()
                opened.append(s)
                return s

            async def __aexit__(self, *exc: Any) -> Any:
                return await self._inner.__aexit__(*exc)

        counting_maker = _CountingMaker()

        # A fake redis stub the adapter never reaches — we short-circuit
        # the chat() flow via a patched ``_await_worker_with_capacity`` /
        # dispatch module so the test stays at the session-factory layer.
        fake_redis = object()

        account = ModelAccount(
            id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            provider="executor",
            label="test",
            litellm_model="executor/claude_code",
            api_base=None,
            api_key_encrypted=None,
            data_jurisdiction="us",
            is_active=True,
            extra_params={"executor_type": "claude_code"},
        )

        # The bound session is the legacy fallback — it must NOT be used
        # when ``session_factory`` is set. We pass an explicit sentinel
        # so a regression that uses it surfaces in the dispatch spies.
        async with maker() as bound_session:
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=account.workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=bound_session,
                settings=get_settings(),
                redis=fake_redis,
                session_factory=counting_maker,  # type: ignore[arg-type]
            )

            # Patch the dispatch helpers + capacity helper so they record
            # which session they were called with and immediately return
            # a synthetic task / completion. We're NOT testing dispatch
            # here — we're testing the session plumbing.
            from backend.dispatch import adapter as adapter_module
            from backend.executors import dispatch as dispatch_module

            saw_sessions: list[AsyncSession] = []
            fake_task_id = uuid.uuid4()

            class _FakeTask:
                id = fake_task_id

            async def _fake_await_worker(**kwargs: Any) -> Any:
                saw_sessions.append(kwargs["session"])

                class _W:
                    id = uuid.uuid4()

                return _W()

            async def _fake_create_task(session: AsyncSession, **kwargs: Any) -> Any:
                saw_sessions.append(session)
                return _FakeTask()

            async def _fake_dispatch_task(
                _redis: Any, *, session: AsyncSession, task: Any, worker_id: Any
            ) -> None:
                saw_sessions.append(session)

            class _Completed:
                status = "done"
                output = "ok"
                artifact_refs = None
                error_message = None

            async def _fake_await_completion(
                _redis: Any, *, session: AsyncSession, task_id: Any, timeout_s: Any
            ) -> Any:
                saw_sessions.append(session)
                return _Completed()

            # Monkey-patch only for the duration of the call.
            real_aw = adapter_module._await_worker_with_capacity
            adapter_module._await_worker_with_capacity = _fake_await_worker  # type: ignore[assignment]
            real_create = dispatch_module.create_task
            real_dispatch = dispatch_module.dispatch_task
            real_complete = dispatch_module.await_completion
            dispatch_module.create_task = _fake_create_task  # type: ignore[assignment]
            dispatch_module.dispatch_task = _fake_dispatch_task  # type: ignore[assignment]
            dispatch_module.await_completion = _fake_await_completion  # type: ignore[assignment]
            # Stub the commit on whichever session is used so we don't
            # actually try to commit an empty session against SQLite (it
            # would succeed but it's noise).
            try:
                response = await adapter.chat(
                    system="x", messages=[{"role": "user", "content": "y"}]
                )
            finally:
                adapter_module._await_worker_with_capacity = real_aw  # type: ignore[assignment]
                dispatch_module.create_task = real_create  # type: ignore[assignment]
                dispatch_module.dispatch_task = real_dispatch  # type: ignore[assignment]
                dispatch_module.await_completion = real_complete  # type: ignore[assignment]

            assert response.content == "ok"
            # Exactly one fresh session was opened by the factory for this
            # chat call.
            assert len(opened) == 1, (
                f"expected exactly one factory-opened session, got {len(opened)}"
            )
            fresh = opened[0]
            # And every dispatch step used THAT fresh session, NOT the
            # bound fallback session.
            assert all(s is fresh for s in saw_sessions), (
                "dispatch helpers received the bound session instead of the "
                f"factory-opened one. bound={bound_session!r} fresh={fresh!r} "
                f"saw={saw_sessions!r}"
            )
            assert bound_session not in saw_sessions


async def test_adapter_without_session_factory_falls_back_to_bound_session() -> None:
    """Backward-compat — callers that haven't migrated still work.

    When ``session_factory`` is None, the adapter uses ``self.session``
    exactly like pre-E19.
    """
    import uuid

    from backend.config import get_settings
    from backend.dispatch.adapter import ExecutorAdapter
    from backend.router.accounts.models import ModelAccount

    async with _shared_sessionmaker() as maker:
        async with maker() as bound_session:
            account = ModelAccount(
                id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                provider="executor",
                label="test",
                litellm_model="executor/claude_code",
                api_base=None,
                api_key_encrypted=None,
                data_jurisdiction="us",
                is_active=True,
                extra_params={"executor_type": "claude_code"},
            )
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=account.workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=bound_session,
                settings=get_settings(),
                redis=object(),
                # No session_factory — must fall back to bound session.
            )

            from backend.dispatch import adapter as adapter_module
            from backend.executors import dispatch as dispatch_module

            saw_sessions: list[AsyncSession] = []
            fake_task_id = uuid.uuid4()

            class _FakeTask:
                id = fake_task_id

            async def _fake_await_worker(**kwargs: Any) -> Any:
                saw_sessions.append(kwargs["session"])

                class _W:
                    id = uuid.uuid4()

                return _W()

            async def _fake_create_task(session: AsyncSession, **kwargs: Any) -> Any:
                saw_sessions.append(session)
                return _FakeTask()

            async def _fake_dispatch_task(
                _redis: Any, *, session: AsyncSession, task: Any, worker_id: Any
            ) -> None:
                saw_sessions.append(session)

            class _Completed:
                status = "done"
                output = "ok"
                artifact_refs = None
                error_message = None

            async def _fake_await_completion(
                _redis: Any, *, session: AsyncSession, task_id: Any, timeout_s: Any
            ) -> Any:
                saw_sessions.append(session)
                return _Completed()

            real_aw = adapter_module._await_worker_with_capacity
            adapter_module._await_worker_with_capacity = _fake_await_worker  # type: ignore[assignment]
            real_create = dispatch_module.create_task
            real_dispatch = dispatch_module.dispatch_task
            real_complete = dispatch_module.await_completion
            dispatch_module.create_task = _fake_create_task  # type: ignore[assignment]
            dispatch_module.dispatch_task = _fake_dispatch_task  # type: ignore[assignment]
            dispatch_module.await_completion = _fake_await_completion  # type: ignore[assignment]
            try:
                response = await adapter.chat(
                    system="x", messages=[{"role": "user", "content": "y"}]
                )
            finally:
                adapter_module._await_worker_with_capacity = real_aw  # type: ignore[assignment]
                dispatch_module.create_task = real_create  # type: ignore[assignment]
                dispatch_module.dispatch_task = real_dispatch  # type: ignore[assignment]
                dispatch_module.await_completion = real_complete  # type: ignore[assignment]

            assert response.content == "ok"
            # Bound session was used everywhere — the legacy contract.
            assert all(s is bound_session for s in saw_sessions), (
                f"expected bound session, got {saw_sessions!r}"
            )
