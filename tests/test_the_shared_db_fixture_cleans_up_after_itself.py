"""The file-backed SQLite fixture must delete its temp dir (#997).

``shared_file_sessionmaker`` creates one ``tempfile.mkdtemp()`` per call and its
docstring used to justify never deleting it:

    "The temp dir is **left for the OS to reap**; the engine is disposed on exit."

Measured on this host 2026-09-17, that premise is false:

===========================  ===========
``bsvibe-shared-test-*`` dirs     18,739
total size                     1,888.9 MB
oldest surviving                2026-08-15  (33 days — nothing reaped it)
created in the last 7 days          5,740  (~820/day)
===========================  ===========

macOS's periodic cleanup touches 3-day-stale *files* under disk pressure; these
directories simply accumulate. 67 call sites use the fixture, so the suite adds
dozens per run.

🚨 The leak also hides itself as it grows. At 18,739 entries the obvious count
blows the argv limit, the shell writes "argument list too long" to stderr, and
``wc -l`` faithfully counts the empty stdout:

    $ ls -d "$TMPDIR"bsvibe-shared-test-* | wc -l
    0                                # ← false; the real answer is 18,739

So the bigger the leak, the more confidently a naive count reports zero. Count
with ``find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'bsvibe-shared-test-*' -type d``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_the_shared_file_fixture_removes_its_temp_dir() -> None:
    """The directory holding ``test.db`` must be gone once the block exits.

    Captured from INSIDE the block rather than reconstructed from a prefix
    glob: globbing ``$TMPDIR`` would pass just as well while leaking, as long
    as SOME other run had cleaned up, and it cannot survive the argv limit
    documented above. The path this call actually used is the only subject.
    """
    async with shared_file_sessionmaker() as maker:
        async with maker() as session:
            assert session is not None
        # The engine's file must exist while the block is open — this is the
        # positive control. Without it, a fixture that never created anything
        # would satisfy the "it is gone afterwards" assertion vacuously.
        engine = maker.kw["bind"]
        db_path = Path(str(engine.url.database or ""))
        assert db_path.exists(), f"fixture never created its DB file at {db_path}"
        tmp_dir = db_path.parent

    assert not tmp_dir.exists(), (
        f"the fixture leaked {tmp_dir} — 18,739 of these were found on the dev host. "
        "The OS does not reap them."
    )
