"""Deterministic extractors — file tree, manifests, top-level docs.

Lift A v2 — the THIRD stage of the bootstrap pipeline. Three small functions
that turn a walked repo into a handful of high-signal artifacts BSage's
IngestCompiler can chunk + classify. NONE of them parses content; raw text
is handed to the LLM with a labelled prefix so it can reason about ``"what
does this dep mean"`` better than any registry we could ship.

Output shape — every artifact is a ``dict[str, object]`` with at minimum
``label`` (used by IngestCompiler to address the seed in its reasoning),
``content`` (the raw text), and ``kind`` (one of the four bucket names —
purely informational; the compiler ignores unknown keys).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from backend.products.application.bootstrap.selector import FileBucket, classify
from backend.products.application.bootstrap.walker import WalkedFile

#: Max depth the file-tree extractor walks. Three levels is enough to
#: show a repo's shape ("backend/ + apps/ + tests/, then their direct
#: children") without dumping a forest.
_FILE_TREE_DEPTH = 3

#: Cap on the manifest content the extractor hands to the LLM. A 500KB
#: ``package-lock.json`` already got filtered out at the lockfile-name
#: check; legitimate manifests are tiny. The cap is a backstop.
_MAX_MANIFEST_CHARS = 32_000

#: Same backstop for top-level docs. Most READMEs are <10KB; we cap
#: well above that to keep the artifact a single LLM seed.
_MAX_DOC_CHARS = 32_000


def file_tree(walked: Sequence[WalkedFile]) -> dict[str, Any]:
    """Markdown file-tree artifact — the SINGLE shape-of-the-repo seed.

    Builds a 3-level-deep tree from the walker's already-filtered output
    (vendored dirs / binaries / oversize files are gone by the time we
    get here) so the LLM sees the project's structure without the noise.

    Output shape: one artifact with kind=``"file-tree"`` and content
    formatted as a Markdown bulleted tree with two-space indents. Empty
    repos (no walked files) still emit a single artifact saying so —
    the canonicalization pipeline reads "no files" as a meaningful signal
    rather than a missing one.
    """
    tree: dict[str, dict[str, Any]] = {}
    for w in walked:
        parts = w.rel_path.split("/")
        if len(parts) > _FILE_TREE_DEPTH:
            parts = parts[:_FILE_TREE_DEPTH]
            parts[-1] = parts[-1] + "/…"
        cursor = tree
        for part in parts:
            cursor = cursor.setdefault(part, {})

    lines: list[str] = ["# Repository file tree (depth 3)"]
    if not tree:
        lines.append("")
        lines.append("(no walked files — repo is empty or fully filtered)")
    else:
        lines.append("")
        _render_tree(tree, lines, indent=0)

    return {
        "label": "repo/file-tree.md",
        "content": "\n".join(lines),
        "kind": "file-tree",
    }


def _render_tree(node: dict[str, Any], lines: list[str], *, indent: int) -> None:
    """Render ``node`` as bullet lines into ``lines`` with two-space indents."""
    for name in sorted(node):
        prefix = "  " * indent
        lines.append(f"{prefix}- {name}")
        child = cast(dict[str, Any], node[name])
        if child:
            _render_tree(child, lines, indent=indent + 1)


def manifests(walked: Sequence[WalkedFile]) -> list[dict[str, Any]]:
    """One artifact per manifest file (raw content, NO parsing).

    Founder decision: the LLM understands ``"requires fastapi>=0.110"``
    better than any hard-coded dep registry — so we hand the manifest
    verbatim instead of pre-parsing into a dep graph. Each artifact's
    label is the repo-relative path so the LLM can reference it; the
    content is prefixed with ``# Manifest: <path>`` so the chunked
    output keeps the source in scope.
    """
    out: list[dict[str, Any]] = []
    for w in walked:
        if classify(w.rel_path) is not FileBucket.STRUCTURAL_MANIFEST:
            continue
        text = _safe_read(w.abs_path, _MAX_MANIFEST_CHARS)
        if text is None:
            continue
        out.append(
            {
                "label": w.rel_path,
                "content": f"# Manifest: {w.rel_path}\n\n{text}",
                "kind": "manifest",
            }
        )
    return out


def docs(walked: Sequence[WalkedFile]) -> list[dict[str, Any]]:
    """One artifact per top-level doc + every ``.bsvibe/*.md``.

    Same prefix convention as :func:`manifests` so the LLM sees the
    source path. Raw content is handed through — markdown is already a
    high-signal format the IngestCompiler chunks well.
    """
    out: list[dict[str, Any]] = []
    for w in walked:
        if classify(w.rel_path) is not FileBucket.STRUCTURAL_DOC:
            continue
        text = _safe_read(w.abs_path, _MAX_DOC_CHARS)
        if text is None:
            continue
        out.append(
            {
                "label": w.rel_path,
                "content": f"# Doc: {w.rel_path}\n\n{text}",
                "kind": "doc",
            }
        )
    return out


def _safe_read(path: Path, cap: int) -> str | None:
    """Read ``path`` as UTF-8 (errors replaced), return ``None`` on FS error.

    The cap is enforced at the READ boundary so we never load a multi-MB
    file into memory just to slice. A truncated read gets a one-line
    trailing marker so the LLM can tell the seed is partial.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(cap + 1)
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if len(text) > cap:
        return text[:cap] + "\n\n…[truncated for ingest seed cap]…"
    return text


__all__ = ["docs", "file_tree", "manifests"]
