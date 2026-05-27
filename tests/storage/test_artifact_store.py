"""Unit tests for the :mod:`backend.storage.artifact_store` Protocol +
:class:`LocalFilesystemArtifactStore` implementation (C3 lift).

These pin the swap-ready surface: future R2/S3 implementations must satisfy
the same four-method contract, and the traversal guard is CENTRALIZED here
(every call site stops needing its own ``is_relative_to`` check)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.storage.artifact_store import (
    ArtifactStore,
    LocalFilesystemArtifactStore,
)


def test_local_store_is_structurally_an_artifact_store(tmp_path: Path) -> None:
    """Protocol conformance — the concrete store satisfies the seam."""
    store: ArtifactStore = LocalFilesystemArtifactStore(tmp_path)
    assert hasattr(store, "run_dir")
    assert hasattr(store, "put")
    assert hasattr(store, "read_bytes")
    assert hasattr(store, "exists")


def test_run_dir_returns_run_scoped_local_path(tmp_path: Path) -> None:
    """``run_dir`` returns the per-run local path (the sandbox / ToolRegistry
    must mount a real ``Path`` — that contract holds for the FS impl)."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    out = store.run_dir(run_id)
    assert out == (tmp_path / str(run_id)).resolve()
    # Parent root exists (creating it on demand is part of the contract — the
    # worker creates the run dir before driving, but ``run_dir`` itself must
    # not error if the parent root has been pre-created).
    assert out.parent == tmp_path.resolve()


def test_put_then_read_bytes_round_trips(tmp_path: Path) -> None:
    """``put`` writes bytes under ``<root>/<run_id>/<ref>`` and ``read_bytes``
    returns the exact same bytes back."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    out = store.put(run_id, "out.txt", b"hello")
    assert out == (tmp_path / str(run_id) / "out.txt").resolve()
    assert out.read_bytes() == b"hello"
    assert store.read_bytes(run_id, "out.txt") == b"hello"


def test_put_creates_parent_directories(tmp_path: Path) -> None:
    """Nested refs (``src/app.py``) auto-create their parent dirs on put."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    out = store.put(run_id, "src/app.py", b"x = 1\n")
    assert out.read_bytes() == b"x = 1\n"
    assert store.read_bytes(run_id, "src/app.py") == b"x = 1\n"


def test_exists_mirrors_disk(tmp_path: Path) -> None:
    """``exists`` is True iff the ref's file is present (an unwritten ref is
    False — the run dir not existing yet is also False, not an error)."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    assert store.exists(run_id, "missing.txt") is False
    store.put(run_id, "present.txt", b"ok")
    assert store.exists(run_id, "present.txt") is True
    assert store.exists(run_id, "still-missing.txt") is False


def test_put_rejects_traversal_ref(tmp_path: Path) -> None:
    """A ``../escape`` ref must raise ``ValueError`` — the traversal guard is
    centralized here so call sites stop writing their own."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    with pytest.raises(ValueError, match="traversal|escape|outside"):
        store.put(run_id, "../escape.txt", b"pwned")
    # And the malicious file was NOT written outside the run root.
    assert not (tmp_path / "escape.txt").exists()


def test_put_rejects_absolute_path(tmp_path: Path) -> None:
    """An absolute ``ref`` like ``/etc/passwd`` is refused."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    with pytest.raises(ValueError, match="absolute|traversal|outside"):
        store.put(run_id, "/etc/passwd", b"pwned")


def test_read_bytes_rejects_traversal_ref(tmp_path: Path) -> None:
    """Reads are guarded too: ``../`` on read raises rather than escaping."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    with pytest.raises(ValueError, match="traversal|escape|outside"):
        store.read_bytes(run_id, "../etc/passwd")


def test_exists_rejects_traversal_ref(tmp_path: Path) -> None:
    """``exists`` on a traversal ref raises (callers must not silently get
    "False" for a malicious ref — the guard is loud)."""
    store = LocalFilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    with pytest.raises(ValueError, match="traversal|escape|outside"):
        store.exists(run_id, "../escape.txt")


def test_run_dir_does_not_leak_across_runs(tmp_path: Path) -> None:
    """Two distinct ``run_id`` values resolve to two distinct sub-trees — no
    cross-run leakage on the path layer."""
    store = LocalFilesystemArtifactStore(tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    store.put(a, "a.txt", b"alpha")
    store.put(b, "b.txt", b"beta")
    assert store.exists(a, "b.txt") is False
    assert store.exists(b, "a.txt") is False
