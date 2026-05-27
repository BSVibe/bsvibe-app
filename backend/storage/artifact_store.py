"""Per-run artifact storage seam.

Founder pattern (BSNexus ``backend/src/core/project_workspace.py``): keep the
surface tiny + the traversal guard CENTRALIZED so the filesystem-backed
implementation can later be swapped for R2/S3 without rewriting call sites.

Today every artifact lives under ``<root>/<run_id>/<ref>`` on a local disk
(``root`` = :attr:`backend.config.Settings.run_workspace_root`). The agent
loop / executor / sandbox + the artifact-read endpoint all touch this tree.
Each previously rebuilt its own ``Path(root)/<run_id>/<ref>.resolve()`` and
``is_relative_to`` guard — duplicated, error-prone, and a barrier to ever
plugging in an object store. This module replaces all of that with one
:class:`ArtifactStore` Protocol + one :class:`LocalFilesystemArtifactStore`.

Future R2/S3 implementations satisfy :class:`ArtifactStore` directly; the
only nuance is :meth:`ArtifactStore.run_dir` — the sandbox + ToolRegistry
*mount* this path, so an object-store impl must stage to a local temp dir
to return a real :class:`pathlib.Path`. Out of scope for this lift (the
founder asked for the swap-ready surface, not the alternate impl).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """The four-method per-run artifact storage seam.

    Implementations:

    * :class:`LocalFilesystemArtifactStore` — today's default; everything
      lives under ``<root>/<run_id>/`` on local disk.
    * (future) ``R2ArtifactStore`` / ``S3ArtifactStore`` — round-trip ``put``
      / ``read_bytes`` against the object store and stage to a local temp
      dir for :meth:`run_dir` so the sandbox can still mount a real
      :class:`pathlib.Path`.

    The traversal guard (every ref MUST resolve inside the run dir, absolute
    paths refused) is the implementation's responsibility — call sites stop
    writing their own ``is_relative_to`` check.
    """

    def run_dir(self, run_id: uuid.UUID) -> Path:
        """Return the per-run local directory.

        For the FS impl this is ``<root>/<run_id>``. For an object-store impl
        this is a locally-staged temp dir (the sandbox + ToolRegistry need a
        real :class:`pathlib.Path` to mount + operate on)."""

    def put(self, run_id: uuid.UUID, ref: str, content: bytes) -> Path:
        """Persist ``content`` at ``<run>/<ref>``. Returns the resolved path.

        Raises :class:`ValueError` for refs that escape the run dir (absolute
        paths, ``..`` traversal). Parent directories are created as needed.
        """

    def read_bytes(self, run_id: uuid.UUID, ref: str) -> bytes:
        """Read the bytes at ``<run>/<ref>``.

        Raises :class:`ValueError` on a traversal/absolute ref. Raises
        :class:`FileNotFoundError` when the ref is absent (callers translate
        to 404 / "no longer available" at their boundary)."""

    def exists(self, run_id: uuid.UUID, ref: str) -> bool:
        """``True`` iff the ref's file is present on disk for the run.

        Raises :class:`ValueError` on a traversal/absolute ref — the guard is
        loud so a malicious ref can't silently masquerade as "False"."""


class LocalFilesystemArtifactStore:
    """The filesystem-backed :class:`ArtifactStore` (today's default).

    Constructed once at startup with a ``root`` (= ``settings.run_workspace_root``)
    and shared across the worker / endpoint as a singleton. All four methods
    centralize the traversal guard via :meth:`_resolve_within_run`.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def run_dir(self, run_id: uuid.UUID) -> Path:
        """Return ``<root>/<run_id>`` resolved (does NOT create the dir — the
        worker creates it on demand before driving, matching the pre-lift
        behaviour exactly)."""
        return (self._root / str(run_id)).resolve()

    def put(self, run_id: uuid.UUID, ref: str, content: bytes) -> Path:
        target = self._resolve_within_run(run_id, ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def read_bytes(self, run_id: uuid.UUID, ref: str) -> bytes:
        target = self._resolve_within_run(run_id, ref)
        return target.read_bytes()

    def exists(self, run_id: uuid.UUID, ref: str) -> bool:
        target = self._resolve_within_run(run_id, ref)
        return target.is_file()

    # ── internals ──────────────────────────────────────────────────────────

    def _resolve_within_run(self, run_id: uuid.UUID, ref: str) -> Path:
        """Resolve ``<run_dir>/<ref>`` and refuse anything that escapes.

        Centralizes the path-traversal defense — every public method routes
        here so a single guard covers ``put`` / ``read_bytes`` / ``exists``.
        Mirrors the pre-lift inline guards (``is_relative_to`` containment
        check, Py 3.9+) + the BSNexus ``project_workspace`` shape (refuse
        absolute paths + ``..`` segments outright)."""
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"invalid ref: {ref!r}")
        # Refuse absolute paths up-front — an absolute ref by definition
        # escapes the run dir (the ``resolve`` step below would also catch
        # most cases, but a loud reject here keeps the failure mode obvious).
        if Path(ref).is_absolute():
            raise ValueError(f"absolute ref refused (escapes run dir): {ref!r}")
        run_dir = self.run_dir(run_id)
        target = (run_dir / ref).resolve()
        # ``is_relative_to`` is the realpath containment check: a ``..``
        # segment that escapes the run dir trips this and the guard rejects.
        if not target.is_relative_to(run_dir):
            raise ValueError(f"traversal ref refused (outside run dir): {ref!r}")
        return target


__all__ = ["ArtifactStore", "LocalFilesystemArtifactStore"]
