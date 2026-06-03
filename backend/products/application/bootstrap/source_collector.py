"""Source collector — raw source files → labelled artifacts.

Lift A v2 — the FOURTH stage of the bootstrap pipeline. Where the structural
extractors slice the repo into "file tree + manifest + doc" seeds, the
source collector just wraps every source file (after walker + selector
filtering) in a ``# File: <path>`` header and hands it through to
``Knowledge.ingest`` as a raw artifact.

The LLM-side compiler (``IngestCompiler.compile_batch``) handles chunking +
classification. We do NOT parse the file. We do NOT walk imports. We do
NOT extract symbols. (Founder decision — see lift design doc; the LLM is
strictly better at "what's in this file" than per-language regex.)

The collector also caps per-file content at a generous-but-bounded size:
the walker already dropped anything >500KB, so this cap is the practical
"don't blow up the LLM batch budget on a single file" backstop.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from backend.products.application.bootstrap.selector import FileBucket, classify
from backend.products.application.bootstrap.walker import WalkedFile

#: Per-source-file content cap (chars after decode). The IngestCompiler
#: will further chunk anything that's still large; this is the upper
#: bound at the artifact stage so one accidentally-checked-in 400KB
#: generated file can't swamp a batch.
_MAX_SOURCE_CHARS = 128_000


def collect_source_artifacts(walked: Sequence[WalkedFile]) -> Iterator[dict[str, Any]]:
    """Yield one artifact per source-bucket file (lazy).

    The artifact's ``label`` is the repo-relative path so the LLM can
    address it in its reasoning; ``content`` is the raw file text with
    a ``# File: <path>`` header so the path stays in scope across the
    compiler's chunk boundary; ``kind`` is ``"source"`` (informational).

    Lazy generator so the orchestrator can stream into a single list
    without materialising twice — the structural pass is small + eager,
    the source pass is large + streamed.
    """
    for w in walked:
        if classify(w.rel_path) is not FileBucket.SOURCE:
            continue
        text = _safe_read(w.abs_path, _MAX_SOURCE_CHARS)
        if text is None:
            continue
        yield {
            "label": w.rel_path,
            "content": f"# File: {w.rel_path}\n\n{text}",
            "kind": "source",
        }


def _safe_read(path: Path, cap: int) -> str | None:
    """Read ``path`` as UTF-8 (errors replaced), capped at ``cap`` chars.

    A truncated read gets a one-line trailing marker so the LLM can tell
    the artifact is partial. FS errors return ``None`` so the caller
    skips the file silently — a bootstrap that's 99% fine shouldn't be
    derailed by one bad inode.
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


__all__ = ["collect_source_artifacts"]
