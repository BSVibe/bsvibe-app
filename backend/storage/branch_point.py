"""Where a run's branch left main — the ONE place that decides it.

The verify gate (``inplace_gate``, which asks a sandbox box) and the deliverable's diff /
changed-file list (``product_workspace``, which asks a local worktree) must measure from the
SAME point, or the founder reviews a diff that disagrees with what was verified. Only the
transport differs, so the decision takes a ``GitQuery`` and each caller supplies its own.

A run's work tree is a reused clone, so local ``main`` can lag ``origin/main`` (seen in a past
run: local ``main`` sat at #1200 while the agent rebased onto an ``origin/main`` holding
#1205). Measuring from the stale ref drags the newer commits into the changed list. With both
merge-bases found, the one that descends from the other is the more recent; one ref alone
answers for itself; unrelated points keep ``MAIN_REFS`` order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: ``(exit_code, stdout)`` of ``git <args>`` run inside the run's tree. A query that could not
#: be answered at all (unreachable machine, timeout) reports a non-zero exit code.
GitQuery = Callable[[tuple[str, ...]], Awaitable[tuple[int, str]]]

MAIN_REFS = ("main", "origin/main")


async def branch_point(query: GitQuery) -> str | None:
    """The merge-base with the main the run actually climbed onto, else ``None``."""
    points = [p for p in [await _merge_base(query, ref) for ref in MAIN_REFS] if p]
    if not points:
        return None
    best = points[0]
    for other in points[1:]:
        if other != best and await _is_ancestor(query, best, other):
            best = other
    return best


async def _is_ancestor(query: GitQuery, older: str, newer: str) -> bool:
    code, _ = await query(("merge-base", "--is-ancestor", older, newer))
    return code == 0


async def _merge_base(query: GitQuery, ref: str) -> str | None:
    code, out = await query(("merge-base", "HEAD", ref))
    lines = out.strip().splitlines()
    if code == 0 and lines and lines[0].strip():
        return lines[0].strip()
    return None


__all__ = ["MAIN_REFS", "GitQuery", "branch_point"]
