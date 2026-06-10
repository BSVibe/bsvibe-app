"""Bootstrap file filter — Lift E20 Phase A.

The standard "packer filter" the bootstrap walk runs every file through
before yielding a :class:`WalkedFile`. Drops the noise that the old
file-dump bootstrap fed to the LLM and turned into 12,199 mostly-useless
notes on the bsvibe-app dogfood:

* ``.gitignore`` entries (when a ``.gitignore`` is present at the repo
  root — we honor only the root file; subdir gitignores are ignored for
  now, the founder hasn't asked for nested support and it's cheap to
  add later)
* lockfiles (``*.lock`` / ``*.lockb`` / named lockfiles)
* vendor + build dirs at any depth (``node_modules``, ``.venv``,
  ``__pycache__``, ``dist``, ``build``, ``target``, ``vendor``,
  ``.next``, ``.nuxt``, ``.cache``, ``.parcel-cache``, ``.git``)
* binary file extensions (images, video, archives, compiled artifacts)
* IDE / OS cruft (``.idea``, ``.vscode``, ``.DS_Store``, ``Thumbs.db``)
* files over a configurable byte cap (default 50KB, much tighter than
  the walker's 500KB per-file ceiling — Graphify ships with this as
  its packer baseline)

The filter is deterministic and CHEAP: every check is membership in a
frozenset, suffix match, or a single :mod:`pathspec` compile (when a
``.gitignore`` is present). It runs inside the walker's hot loop, so the
cost matters.

:meth:`BootstrapFileFilter.summary` returns the per-reason drop counters
the walker's structured log surfaces — the founder sees exactly what
was filtered without having to grep the FS.

Lift E20 Phase A § "Filter": the new bootstrap pipeline wires this
filter into the walker via the ``file_filter=`` parameter; the legacy
walker path (no filter) still works for the M0 / settle paths that
have no reason to enforce this aggressive cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any


#: Default per-file byte cap. Tighter than the walker's 500KB ceiling so
#: a 100KB minified-but-not-binary blob still drops before it hits the
#: graph builder. Graphify's packer baseline runs ~50KB; we mirror it.
DEFAULT_MAX_FILE_BYTES = 50 * 1024

#: Lockfile filenames (case-sensitive — matches what's actually on disk
#: in the repos the founder is bootstrapping). Suffix matchers
#: (``*.lock`` / ``*.lockb``) catch the rest.
_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
    }
)
_LOCKFILE_SUFFIXES: tuple[str, ...] = (".lock", ".lockb")

#: Vendor / build / IDE-cache directory names. Match anywhere in the
#: relative path, not just at the root.
_VENDOR_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".git",
        ".hg",
        ".svn",
        "dist",
        "build",
        "out",
        "target",
        "vendor",
        "__pycache__",
        ".next",
        ".nuxt",
        ".cache",
        ".parcel-cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".turbo",
        ".svelte-kit",
        ".vercel",
        ".gradle",
        ".bundle",
        ".serverless",
        ".angular",
        ".direnv",
        ".pnpm-store",
        "bower_components",
        "coverage",
    }
)

#: Binary file extensions. Matched on a lowercased suffix so
#: ``logo.PNG`` and ``logo.png`` both drop.
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".bmp",
        ".tiff",
        ".tif",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".wav",
        ".flac",
        ".ogg",
        ".webm",
        ".m4a",
        ".m4v",
        ".zip",
        ".tar",
        ".tgz",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".bin",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".o",
        ".a",
        ".class",
        ".pyc",
        ".pyo",
        ".jar",
        ".war",
        ".ear",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
    }
)

#: IDE / OS cruft directory + file names. Anywhere in the path.
_IDE_CRUFT_DIRS: frozenset[str] = frozenset({".idea", ".vscode"})
_IDE_CRUFT_FILES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})


class FilterReason(StrEnum):
    """Per-decision result the filter returns when it drops a file.

    Returning ``None`` from :meth:`BootstrapFileFilter.decide` means
    "keep this file". A non-None :class:`FilterReason` means "drop, and
    bump the corresponding counter for the audit log."
    """

    GITIGNORE = "gitignore"
    LOCKFILE = "lockfile"
    VENDOR_DIR = "vendor_dir"
    BINARY_EXTENSION = "binary_extension"
    IDE_CRUFT = "ide_cruft"
    OVERSIZE = "oversize"


@dataclass
class BootstrapFileFilter:
    """Per-bootstrap-run filter that decides keep/drop for each walked file.

    Construct once per bootstrap (the walker holds the instance for the
    duration of the walk so the counters aggregate the whole repo).
    Call :meth:`decide` for every candidate file; consult
    :meth:`summary` after the walk for the structured log.

    The instance is stateful (counters mutate on every drop) but the
    walker is single-threaded per repo, so no locking is needed.
    """

    repo_root: Path
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    _counters: dict[FilterReason, int] = field(default_factory=dict)
    _gitignore_spec: Any | None = field(default=None, init=False, repr=False)
    _gitignore_loaded: bool = field(default=False, init=False, repr=False)

    def _load_gitignore(self) -> Any | None:
        """Lazy-load the repo-root .gitignore once per filter instance.

        Missing file → ``None`` (cheap fast path on every decide call).
        Returns the compiled :class:`pathspec.PathSpec` ready for
        per-file ``match_file`` queries.
        """
        if self._gitignore_loaded:
            return self._gitignore_spec
        self._gitignore_loaded = True
        gi = self.repo_root / ".gitignore"
        try:
            text = gi.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        try:
            import pathspec  # noqa: PLC0415 — only on first decide call

            # ``gitignore`` is the modern pattern factory in pathspec ≥0.12
            # (replaced the deprecated ``gitwildmatch``). Falls back to the
            # legacy name when an older wheel sneaks in, so this code stays
            # working on the founder's host without pinning.
            try:
                self._gitignore_spec = pathspec.PathSpec.from_lines("gitignore", text.splitlines())
            except Exception:  # noqa: BLE001 — old pathspec lacks "gitignore"
                self._gitignore_spec = pathspec.PathSpec.from_lines(
                    "gitwildmatch", text.splitlines()
                )
        except Exception:  # noqa: BLE001 — a bad gitignore must not abort bootstrap
            self._gitignore_spec = None
        return self._gitignore_spec

    def decide(  # noqa: PLR0911 — each return guards one filter category
        self, rel_path: str, *, size: int
    ) -> FilterReason | None:
        """Return ``None`` to keep ``rel_path``, or the reason it was dropped.

        ``rel_path`` is the POSIX-style repo-relative path (the same
        shape :class:`WalkedFile.rel_path` carries). ``size`` is the
        on-disk byte size — used only for the oversize check.

        Order matters: the most common drop reasons sit early so the hot
        path on a large repo (lockfile, vendor dir, gitignore) returns
        without doing the full extension check.
        """
        path = PurePosixPath(rel_path)
        parts = path.parts
        name = path.name

        # Vendor / IDE-cache dirs — match anywhere in the relative path.
        for part in parts[:-1]:  # exclude the filename itself
            if part in _VENDOR_DIRS:
                return self._bump(FilterReason.VENDOR_DIR)
            if part in _IDE_CRUFT_DIRS:
                return self._bump(FilterReason.IDE_CRUFT)

        # IDE / OS cruft files (.DS_Store anywhere, Thumbs.db anywhere).
        if name in _IDE_CRUFT_FILES:
            return self._bump(FilterReason.IDE_CRUFT)

        # Lockfiles — by exact filename or by suffix.
        if name in _LOCKFILE_NAMES:
            return self._bump(FilterReason.LOCKFILE)
        lower_name = name.lower()
        if any(lower_name.endswith(s) for s in _LOCKFILE_SUFFIXES):
            return self._bump(FilterReason.LOCKFILE)

        # Binary extensions.
        suffix = path.suffix.lower()
        if suffix in _BINARY_EXTENSIONS:
            return self._bump(FilterReason.BINARY_EXTENSION)

        # Oversize.
        if size > self.max_file_bytes:
            return self._bump(FilterReason.OVERSIZE)

        # .gitignore at repo root.
        spec = self._load_gitignore()
        if spec is not None and spec.match_file(rel_path):
            return self._bump(FilterReason.GITIGNORE)

        return None

    def _bump(self, reason: FilterReason) -> FilterReason:
        self._counters[reason] = self._counters.get(reason, 0) + 1
        return reason

    def summary(self) -> dict[str, int]:
        """Per-reason drop counts for the bootstrap audit log.

        Keys are the string values of :class:`FilterReason` (so
        ``"lockfile"``, ``"vendor_dir"``, ``"binary_extension"``,
        ``"ide_cruft"``, ``"oversize"``, ``"gitignore"``). Only reasons
        that fired at least once are present — a zero-drop bootstrap
        returns ``{}``.
        """
        return {r.value: n for r, n in self._counters.items()}


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "BootstrapFileFilter",
    "FilterReason",
]
