"""How big a deliverable's change is — file count, busiest top-level dirs, a warning.

The founder approving a Safe Mode item sees the summary, not the diff, so a deliverable
that swept in a sandbox package store (18,752 files, 18,745 of them ``.pnpm-store/``)
looked like any other card. The count comes from the deliverable's own ``artifact_refs``;
a deliverable with none (an old row, a non-code output) reports NOTHING rather than "0".
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


def footprint_of(refs: Iterable[str], *, threshold: int) -> ChangeFootprint:
    """Summarise ``refs``; more than ``threshold`` files is a ``large_change``."""
    ref_list = list(refs)
    if not ref_list:
        return ChangeFootprint()
    counts = Counter(_top_dir(r) for r in ref_list)
    # Busiest first; name as the tiebreaker so the order is stable.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_DIRS_LIMIT]
    total = len(ref_list)
    large = total > threshold
    warning = None
    if large:
        dirs = ", ".join(f"{name} ({n:,})" for name, n in ranked)
        warning = (
            f"큰 변경: 파일 {total:,}개를 바꿉니다 (기준 {threshold:,}개 초과). "
            f"가장 많은 디렉터리: {dirs}. 승인하기 전에 의도한 변경인지 확인하세요."
        )
    return ChangeFootprint(
        changed_files=total,
        top_dirs=[TopDir(dir=name, count=n) for name, n in ranked],
        large_change=large,
        large_change_warning=warning,
    )


__all__ = ["ChangeFootprint", "TopDir", "footprint_of"]
