"""Bootstrap file classifier — structural / source / skip.

Lift A v2 — the SECOND stage of the bootstrap pipeline. Splits the walker's
output into three buckets:

* ``structural`` — deterministic-parser inputs: manifests + top-level docs.
  The extractors module turns these into typed artifacts with stable
  prefixes (``# Manifest: ...``, no LLM parsing of the actual content —
  raw text is faster + the LLM understands "what does this dep mean"
  better than any registry we could hard-code).
* ``source`` — raw source files. Source-collector wraps each in a
  ``# File: path``-prefixed artifact and hands it straight to
  ``Knowledge.ingest``; the IngestCompiler chunks + classifies. NO
  per-language AST. (Founder decision — see lift design doc.)
* ``skip`` — everything else: images, lockfiles, ``.env.example``,
  arbitrary binary outputs the walker missed. We intentionally drop
  these rather than feed them to the LLM as noise.

The allowlist for source extensions is GENEROUS but bounded — any
language BSVibe touches today is here. A future repo with a niche
language file just gets its file skipped (and the LLM still sees the
manifest + the file tree, so the project still appears in the graph
with structural edges).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath

#: Filenames the structural bucket cares about. Each matches at the
#: BASE-NAME level (so ``backend/pyproject.toml`` and root
#: ``pyproject.toml`` both qualify). ``.gitignore`` is intentionally NOT
#: here — it's not signal about the project's purpose.
_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "Pipfile.lock",  # lock excluded by name check below
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "mix.exs",
        "Makefile",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)

#: Lockfiles we explicitly drop from the manifest bucket (they're huge
#: and add no design-time signal). Kept separate so the manifest set
#: above can remain a simple membership check.
_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
    }
)

#: Top-level doc filenames (case-insensitive prefix match). README +
#: ARCHITECTURE + CLAUDE-style instructions + CONTRIBUTING.
_TOP_LEVEL_DOC_PREFIXES: tuple[str, ...] = (
    "README",
    "ARCHITECTURE",
    "CLAUDE",
    "CONTRIBUTING",
    "CHANGELOG",
    "LICENSE",
    "CODE_OF_CONDUCT",
)

#: Extensions the source bucket accepts. Generous — a niche language
#: not on this list gets ``skip``-ed (still indirectly visible via the
#: file tree). All entries include the leading dot for direct
#: ``.suffix`` comparison.
_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyx",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".clj",
        ".cljs",
        ".rb",
        ".php",
        ".cs",
        ".vb",
        ".fs",
        ".swift",
        ".m",
        ".mm",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cxx",
        ".hxx",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".lhs",
        ".ml",
        ".mli",
        ".nim",
        ".zig",
        ".v",
        ".d",
        ".cr",
        ".lua",
        ".pl",
        ".pm",
        ".r",
        ".jl",
        ".dart",
        ".groovy",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
        ".thrift",
        ".vue",
        ".svelte",
        ".astro",
        ".tf",
        ".hcl",
    }
)


class FileBucket(StrEnum):
    """Three-way classifier the walker's output flows through."""

    STRUCTURAL_MANIFEST = "structural-manifest"
    STRUCTURAL_DOC = "structural-doc"
    SOURCE = "source"
    SKIP = "skip"


def classify(rel_path: str) -> FileBucket:
    """Bucket a single walked file by its repo-relative POSIX path.

    Decision order:

    1. Manifest filename (root or sub-dir) → ``STRUCTURAL_MANIFEST``.
    2. Lockfile filename → ``SKIP`` (caught before the doc/source
       checks could match on extension).
    3. Top-level doc (depth==1, name starts with a doc prefix,
       optionally with ``.md`` / ``.rst`` / no extension) →
       ``STRUCTURAL_DOC``. Sub-dir docs aren't promoted — they're
       handled by the source bucket if they're ``.md``, otherwise
       skipped.
    4. ``.bsvibe/*.md`` → ``STRUCTURAL_DOC`` (BSVibe's own metadata
       drop the founder commits into the workspace).
    5. Source extension allow-list → ``SOURCE``.
    6. Otherwise → ``SKIP``.
    """
    path = PurePosixPath(rel_path)
    name = path.name
    if name in _LOCKFILE_NAMES:
        return FileBucket.SKIP
    if name in _MANIFEST_NAMES:
        return FileBucket.STRUCTURAL_MANIFEST
    parts = path.parts
    depth = len(parts)
    upper_stem = path.stem.upper()
    is_top_level = depth == 1
    if is_top_level and any(upper_stem.startswith(p) for p in _TOP_LEVEL_DOC_PREFIXES):
        return FileBucket.STRUCTURAL_DOC
    if depth >= 2 and parts[0] == ".bsvibe" and path.suffix.lower() == ".md":
        return FileBucket.STRUCTURAL_DOC
    if path.suffix.lower() in _SOURCE_EXTENSIONS:
        return FileBucket.SOURCE
    return FileBucket.SKIP


__all__ = ["FileBucket", "classify"]
