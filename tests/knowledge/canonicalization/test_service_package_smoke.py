"""Lift L2 smoke test — verify the canonicalization service package layout.

Per v8 §17.4: `service.py` (1158 LOC) decomposed into a `service/` package with
5 sub-files. Public import path MUST remain unchanged.

This smoke test asserts:
1. Public import `from backend.knowledge.canonicalization.service import CanonicalizationService` still works.
2. The split is a package, not a single module.
3. The 5 sub-modules exist and stay below the 400 LOC budget.
4. No `async with lock.guard(...)` critical section is split across modules
   (invariant from BSage canon spec — single-writer per action_path).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _pkg_dir() -> Path:
    from backend.knowledge.canonicalization import service as service_pkg

    pkg_file = service_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent


class TestPackageLayout:
    def test_public_import_preserved(self) -> None:
        """The historic import path must still resolve."""
        from backend.knowledge.canonicalization.service import CanonicalizationService

        assert CanonicalizationService.__name__ == "CanonicalizationService"

    def test_service_is_now_a_package(self) -> None:
        """service should be a package, not a single .py module."""
        pkg = _pkg_dir()
        assert pkg.name == "service"
        assert (pkg / "__init__.py").exists()

    def test_expected_submodules_exist(self) -> None:
        """The 5 sub-modules per v8 §17.4."""
        pkg = _pkg_dir()
        for name in (
            "_validators.py",
            "_effects.py",
            "_safe_mode.py",
            "_proposal_lifecycle.py",
            "_apply_pipeline.py",
        ):
            assert (pkg / name).exists(), f"missing submodule {name}"

    def test_each_submodule_under_400_loc(self) -> None:
        """Per v8 §17.4 budget: every sub-file ≤ 400 LOC."""
        pkg = _pkg_dir()
        for path in pkg.glob("*.py"):
            loc = sum(1 for _ in path.read_text().splitlines())
            assert loc <= 400, f"{path.name}: {loc} LOC exceeds 400"

    def test_no_lock_guard_split_across_modules(self) -> None:
        """BSage invariant: action_path mutex critical sections stay inside ONE function.

        We assert that within each sub-module, every `async with` lock guard
        opens and (by Python syntax) closes within the same function. Because
        Python `async with` blocks cannot span a function boundary, the structural
        guarantee is "the lock acquisition is in the same file as its body";
        we verify by counting `async with self._lock.guard(` per file and
        ensuring those calls are balanced by `await self._store.write_action`
        or equivalent persist calls in the same file.
        """
        pkg = _pkg_dir()
        for path in pkg.glob("*.py"):
            text = path.read_text()
            guards = text.count("self._lock.guard(")
            if guards == 0:
                continue
            # Critical-section assertion: any file that acquires the lock
            # MUST also persist within the same file. This catches a refactor
            # that delegates the persist step to a helper in another module
            # while still holding the lock from this file.
            assert "self._store.write" in text or "self._store.set" in text, (
                f"{path.name} acquires lock guard but does no store write — "
                "critical section may be split across modules"
            )


@pytest.mark.asyncio
async def test_canonicalization_service_constructs() -> None:
    """End-to-end smoke: construct a real service from the new package."""
    import tempfile

    from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
    from backend.knowledge.canonicalization.service import CanonicalizationService
    from backend.knowledge.canonicalization.store import NoteStore
    from backend.knowledge.graph.storage import FileSystemStorage

    with tempfile.TemporaryDirectory() as tmp:
        svc = CanonicalizationService(
            store=NoteStore(FileSystemStorage(Path(tmp))),
            lock=AsyncIOMutationLock(),
        )
        # Public method exists and is callable
        assert callable(svc.create_action_draft)
        assert callable(svc.apply_action)
        assert callable(svc.approve_action)
        assert callable(svc.reject_action)
        assert callable(svc.expire_stale)
        assert callable(svc.resolve_and_canonicalize)
        assert callable(svc.accept_proposal)
        assert callable(svc.reject_proposal)
