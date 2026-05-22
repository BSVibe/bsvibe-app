"""Tests for AsyncIOMutationLock — per-action-path single-writer (Handoff §0.11)."""

from __future__ import annotations

import asyncio

import pytest

from backend.knowledge.canonicalization.lock import AsyncIOMutationLock


class TestAcquireRelease:
    @pytest.mark.asyncio
    async def test_basic_acquire_release(self) -> None:
        lock = AsyncIOMutationLock()
        token = await lock.acquire("actions/create-concept/x.md")
        assert token.action_path == "actions/create-concept/x.md"
        await lock.release(token)

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        lock = AsyncIOMutationLock()
        async with lock.guard("actions/create-concept/x.md") as token:
            assert token.action_path == "actions/create-concept/x.md"

    @pytest.mark.asyncio
    async def test_different_paths_dont_block_each_other(self) -> None:
        lock = AsyncIOMutationLock()
        order: list[str] = []

        async def hold(path: str, name: str) -> None:
            async with lock.guard(path):
                order.append(f"{name}-enter")
                await asyncio.sleep(0.05)
                order.append(f"{name}-exit")

        await asyncio.gather(
            hold("actions/create-concept/a.md", "A"),
            hold("actions/create-concept/b.md", "B"),
        )
        # Both should run in parallel — at minimum, both enter before either exits.
        assert order.index("A-enter") < order.index("B-exit")
        assert order.index("B-enter") < order.index("A-exit")

    @pytest.mark.asyncio
    async def test_same_path_serializes(self) -> None:
        lock = AsyncIOMutationLock()
        order: list[str] = []

        async def hold(name: str) -> None:
            async with lock.guard("actions/create-concept/same.md"):
                order.append(f"{name}-enter")
                await asyncio.sleep(0.02)
                order.append(f"{name}-exit")

        await asyncio.gather(hold("A"), hold("B"))
        # Strict serialization: A's exit must come before B's enter, or vice versa
        a_enter = order.index("A-enter")
        a_exit = order.index("A-exit")
        b_enter = order.index("B-enter")
        b_exit = order.index("B-exit")
        # No interleaving
        assert (a_exit < b_enter) or (b_exit < a_enter)


class TestReleaseInvariants:
    @pytest.mark.asyncio
    async def test_release_with_unknown_token_raises(self) -> None:
        from datetime import datetime

        from backend.knowledge.canonicalization.lock import AcquiredLock

        lock = AsyncIOMutationLock()
        bogus = AcquiredLock(action_path="nope.md", token="abc", acquired_at=datetime(2026, 5, 6))
        with pytest.raises(RuntimeError, match="unknown action path"):
            await lock.release(bogus)

    @pytest.mark.asyncio
    async def test_release_twice_raises(self) -> None:
        lock = AsyncIOMutationLock()
        token = await lock.acquire("actions/create-concept/x.md")
        await lock.release(token)
        with pytest.raises(RuntimeError, match="not held|unknown action path"):
            await lock.release(token)

    @pytest.mark.asyncio
    async def test_guard_releases_on_exception(self) -> None:
        lock = AsyncIOMutationLock()

        with pytest.raises(RuntimeError, match="boom"):
            async with lock.guard("actions/create-concept/x.md"):
                raise RuntimeError("boom")

        # Should be re-acquirable after exception
        async with lock.guard("actions/create-concept/x.md"):
            pass
