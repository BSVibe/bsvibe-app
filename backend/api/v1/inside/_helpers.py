"""Body-text helpers for the ``/api/v1/inside`` surface (Lift M1).

Cap constants + the small text utilities (``_excerpt``, ``_capped_body``) the
endpoint modules share — extracted so each endpoint stays a thin adapter.
"""

from __future__ import annotations

# Conservative caps — the Inside surface is a calm snapshot, not a data dump.
_DEFAULT_CONCEPT_LIMIT = 50
_MAX_CONCEPT_LIMIT = 200
_DEFAULT_OBSERVATION_LIMIT = 25
_MAX_OBSERVATION_LIMIT = 100

# Force-directed view cap — a calm picture, not the whole graph. When the
# workspace graph exceeds this, keep the most-connected nodes (top-N by degree)
# so the founder sees the structurally important hubs.
_MAX_GRAPH_NODES = 200

# Excerpt cap — a short, founder-legible blurb, not the full note body.
_EXCERPT_CHARS = 200

# Note-body cap (~8KB) — the inspector renders the full observation body, but
# the wire stays bounded; a longer body is truncated and flagged.
_OBSERVATION_BODY_CHARS = 8192


# Deterministic settle-note footer the SettleWorker appends after the LLM
# narrative (see backend/workers/settle_worker.py ``_observation_body``). These
# are machine metadata, not content — the inspector shows the founder the
# narrative, so the footer (a trailing block) is trimmed off.
_SETTLE_FOOTER_PREFIXES = ("Product:", "Intent:", "## Artifacts", "Verified:", "Run:")


def _excerpt(body: str) -> str:
    """First non-empty body line (after the H1), truncated for a calm blurb."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:_EXCERPT_CHARS]
    return ""


def _strip_settle_footer(lines: list[str]) -> list[str]:
    """Drop the trailing settle-note metadata footer. The footer is a contiguous
    block at the end starting with one of ``_SETTLE_FOOTER_PREFIXES``, so cut
    from the first such marker line onward (keeps the LLM narrative above it)."""
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_SETTLE_FOOTER_PREFIXES):
            return lines[:i]
    return lines


def _capped_body(body: str) -> tuple[str, bool]:
    """Full note body capped at ``_OBSERVATION_BODY_CHARS``; flag if truncated.

    The leading H1 is dropped (the inspector already shows it as the note's
    title, so repeating it would be redundant) and the SettleWorker's machine
    footer (Product/Intent/Artifacts/Verified/Run) is trimmed so the inspector
    shows the founder the note's CONTENT, not the metadata. Inner line breaks are
    preserved so it renders as a readable note; only surrounding whitespace is
    stripped.
    """
    lines = body.splitlines()
    start = 0
    # Skip leading blank lines (real settle notes put a blank line between the
    # frontmatter and the H1), then drop a single leading H1 heading + the blank
    # lines beneath it.
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].lstrip().startswith("# "):
        start += 1
        while start < len(lines) and not lines[start].strip():
            start += 1
    text = "\n".join(_strip_settle_footer(lines[start:])).strip()
    if len(text) > _OBSERVATION_BODY_CHARS:
        return text[:_OBSERVATION_BODY_CHARS], True
    return text, False


__all__ = [
    "_DEFAULT_CONCEPT_LIMIT",
    "_DEFAULT_OBSERVATION_LIMIT",
    "_EXCERPT_CHARS",
    "_MAX_CONCEPT_LIMIT",
    "_MAX_GRAPH_NODES",
    "_MAX_OBSERVATION_LIMIT",
    "_OBSERVATION_BODY_CHARS",
    "_capped_body",
    "_excerpt",
]
