"""How big a deliverable's change is — file count, busiest top-level dirs, a warning.

The founder approving a Safe Mode item sees the summary, not the diff, so a deliverable
that swept in a sandbox package store (18,752 files, 18,745 of them ``.pnpm-store/``)
looked like any other card. The count is what delivery will commit (the run worktree, recorded at verify time as
``payload['worktree_change']``); ``artifact_refs`` only lists files written through the work
tools, so it is the fallback when no worktree count was recorded. A deliverable with neither (an old row, a non-code output) reports NOTHING rather than "0".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

TOP_DIRS_LIMIT = 5

#: Bucket for files that sit at the repo root (no directory component).
ROOT_DIR = "."


class TopDir(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str
    count: int


class ChangeFootprint(BaseModel):
    """The fields spliced into a report / Safe Mode item. ``changed_files`` is ``None`` and
    the rest empty when the deliverable declared no ``artifact_refs``."""

    model_config = ConfigDict(extra="forbid")

    changed_files: int | None = None
    top_dirs: list[TopDir] = []
    large_change: bool = False
    large_change_warning: str | None = None


def _top_dir(ref: str) -> str:
    path = ref.replace("\\", "/").lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    head, sep, _ = path.partition("/")
    return head if sep and head else ROOT_DIR


def top_dirs_of(refs: Iterable[str]) -> list[TopDir]:
    """The :data:`TOP_DIRS_LIMIT` busiest top-level directories of ``refs``."""
    counts = Counter(_top_dir(r) for r in refs)
    # Busiest first; name as the tiebreaker so the order is stable.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DIRS_LIMIT]
    return [TopDir(dir=name, count=n) for name, n in ranked]


def footprint_from_counts(total: int, top_dirs: list[TopDir], *, threshold: int) -> ChangeFootprint:
    """Footprint from an already-counted change; more than ``threshold`` files is large."""
    large = total > threshold
    warning = None
    if large:
        dirs = ", ".join(f"{d.dir} ({d.count:,})" for d in top_dirs)
        warning = (
            f"큰 변경: 파일 {total:,}개를 바꿉니다 (기준 {threshold:,}개 초과). "
            f"가장 많은 디렉터리: {dirs}. 승인하기 전에 의도한 변경인지 확인하세요."
        )
    return ChangeFootprint(
        changed_files=total, top_dirs=top_dirs, large_change=large, large_change_warning=warning
    )


def footprint_of(refs: Iterable[str], *, threshold: int) -> ChangeFootprint:
    """Summarise ``refs``; more than ``threshold`` files is a ``large_change``."""
    ref_list = list(refs)
    if not ref_list:
        return ChangeFootprint()
    return footprint_from_counts(len(ref_list), top_dirs_of(ref_list), threshold=threshold)


__all__ = ["ChangeFootprint", "TopDir", "footprint_from_counts", "footprint_of", "top_dirs_of"]
