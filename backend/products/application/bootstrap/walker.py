"""Bootstrap repo walker — selective filesystem traversal with caps.

Lift A v2 — the FIRST stage of the bootstrap pipeline. Walks a cloned repo
root, dropping the directories and files that have no business being fed to
the LLM (vendored dependency trees, build outputs, lockfiles, binaries) and
emitting the rest as :class:`WalkedFile` records the selector then classifies.

Design notes (founder-locked):

* No language-specific analysis here. The walker only knows about file
  extensions for binary detection; the selector decides "structural vs
  source vs skip" downstream.
* Hard caps are enforced WHOLE-repo, not per-file. A repo over the
  ``max_total_bytes`` / ``max_file_count`` thresholds raises
  :class:`BootstrapTooLargeError` at the orchestrator boundary so the
  founder sees ``bootstrap_status="failed:too_large"`` instead of a
  partial ingest.
* Symlinks are followed only when the target stays inside the repo root
  (resolve + ``Path.relative_to``). Outside-repo links are skipped — a
  malicious repo can't make us read ``/etc/passwd`` via a symlink.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Per-file size cap. Files larger than this are silently skipped —
#: indexing 5MB minified bundles or fixture blobs has no useful signal
#: and would dominate the LLM's batch budget.
DEFAULT_MAX_FILE_BYTES = 500 * 1024

#: Whole-repo size cap. Above this, the orchestrator surfaces a typed
#: failure rather than starting an ingest that would never finish on
#: a local LLM. 100MB is comfortable for normal app repos and excludes
#: monorepos with vendored binary assets.
DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024

#: Whole-repo file count cap. Mirrors the byte cap (a repo with 10k+
#: source files is past the design's "one product, one repo" target).
DEFAULT_MAX_FILE_COUNT = 10_000

#: First-N bytes the binary heuristic samples. A NUL byte in the first
#: 8KB is a strong enough signal for "not text" without reading the
#: whole file.
_BINARY_SAMPLE_BYTES = 8 * 1024


#: Directory names that get pruned at the walker. Vendored deps + build
#: outputs + IDE caches. ``set`` for O(1) membership test.
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".turbo",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".vercel",
        ".terraform",
        "node_modules",
        "bower_components",
        "vendor",
        ".venv",
        "venv",
        "env",
        ".tox",
        "__pycache__",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        ".coverage",
        ".gradle",
        ".bundle",
        ".serverless",
        ".parcel-cache",
        ".angular",
        ".direnv",
        ".pnpm-store",
    }
)


@dataclass(frozen=True, slots=True)
class WalkedFile:
    """One file the walker yielded.

    ``rel_path`` is the POSIX-style path relative to the repo root (always
    forward-slashed, even on Windows runners — the LLM sees a stable shape).
    ``size`` is the file's byte size at walk time.
    """

    abs_path: Path
    rel_path: str
    size: int


class BootstrapTooLargeError(RuntimeError):
    """Repo exceeded the whole-repo caps before any artifact was emitted.

    Carries the breached metric (``"bytes"`` or ``"files"``) and the value
    so the runtime layer can write a precise ``bootstrap_error`` row.
    """

    def __init__(self, *, metric: str, value: int, limit: int) -> None:
        self.metric = metric
        self.value = value
        self.limit = limit
        super().__init__(f"repo too large to bootstrap: {metric}={value} exceeds limit {limit}")


def _is_within(child: Path, root: Path) -> bool:
    """``True`` when ``child`` (resolved) stays inside ``root`` (resolved).

    A robust symlink-aware containment check — ``Path.relative_to`` raises
    ``ValueError`` when the resolved child escapes, which we translate to
    ``False``. Both paths are resolved up front so symlinks pointing out
    of the repo can't fool us.
    """
    try:
        child.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _looks_binary(path: Path) -> bool:
    """Heuristic: a NUL byte in the first 8KB → binary.

    Reads at most :data:`_BINARY_SAMPLE_BYTES`. A read error (permission /
    transient FS issue) is treated as binary so we don't crash the walk on
    a single bad file — the ingest skips it and moves on.
    """
    try:
        with path.open("rb") as handle:
            chunk = handle.read(_BINARY_SAMPLE_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def walk_repo(
    repo_root: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
) -> Iterator[WalkedFile]:
    """Walk ``repo_root`` lazily, yielding :class:`WalkedFile` records.

    Pruning happens IN-LOOP so a huge ``node_modules`` is never descended.
    Caps are enforced cumulatively as the iterator advances; the first
    overage raises :class:`BootstrapTooLargeError` immediately — callers
    that want to "see what's in the repo first" should iterate fully
    (the caps are designed to halt before the walk ever produces useful
    work on a too-large repo, so this is the natural fail-fast).

    Yields are deterministic: directories sort alphabetically before files
    within each level, which gives the LLM (downstream) a stable artifact
    order for the same repo across runs.
    """
    root = repo_root.resolve(strict=False)
    counters = _WalkCounters()

    stack: list[Path] = [root]
    while stack:
        current = stack.pop(0)
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            # Unreadable dir → skip silently. The walk continues; we don't
            # let a single bad sub-tree crash a 99%-fine ingest.
            continue
        for entry in entries:
            yield from _handle_entry(
                entry,
                root=root,
                stack=stack,
                skip_dirs=skip_dirs,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
                max_file_count=max_file_count,
                counters=counters,
            )


@dataclass(slots=True)
class _WalkCounters:
    """Running totals the walker checks against the whole-repo caps."""

    total_bytes: int = 0
    total_files: int = 0


def _handle_entry(  # noqa: PLR0911 — per-entry early-returns each guard one filter
    entry: Path,
    *,
    root: Path,
    stack: list[Path],
    skip_dirs: frozenset[str],
    max_file_bytes: int,
    max_total_bytes: int,
    max_file_count: int,
    counters: _WalkCounters,
) -> Iterator[WalkedFile]:
    """Handle one ``entry`` from the walker's per-dir loop.

    Pulled out of :func:`walk_repo` to keep that function's cyclomatic
    complexity inside the linter's ceiling — the per-entry decision tree
    has 8+ branches by itself.
    """
    if entry.is_symlink() and not _is_within(entry, root):
        return
    if entry.is_dir():
        if entry.name in skip_dirs:
            return
        stack.append(entry)
        return
    if not entry.is_file():
        return
    try:
        size = entry.stat().st_size
    except OSError:
        return
    if size > max_file_bytes:
        return
    if _looks_binary(entry):
        return
    counters.total_bytes += size
    counters.total_files += 1
    if counters.total_files > max_file_count:
        raise BootstrapTooLargeError(
            metric="files", value=counters.total_files, limit=max_file_count
        )
    if counters.total_bytes > max_total_bytes:
        raise BootstrapTooLargeError(
            metric="bytes", value=counters.total_bytes, limit=max_total_bytes
        )
    try:
        rel = entry.relative_to(root)
    except ValueError:
        return
    yield WalkedFile(
        abs_path=entry,
        rel_path=rel.as_posix(),
        size=size,
    )


__all__ = [
    "BootstrapTooLargeError",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILE_COUNT",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_SKIP_DIRS",
    "WalkedFile",
    "walk_repo",
]
