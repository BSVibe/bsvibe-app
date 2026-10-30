"""What verification leaves in the checkout stays out of the run's commits (#1267).

Verification runs the repo's own tools in the run's checkout, and tools leave things behind:
caches, stores, logs. The next ``git add -A`` (``commit_worktree``, the delivery's
``commit_all``) then commits them as the run's work. Prod #1263 (run ``14e222a1``): the final
check's ``pnpm install`` put pnpm's store at the sandbox mount root and 18,745 of its files
reached the run branch. That came from the sandbox's mount, not from the stack — an ignore list
keyed on tool names cannot foresee it.

So the untracked, not-ignored entries that APPEAR during verification are written, anchored,
into that checkout's ``.git/info/exclude``:

* not deleted — a file verification created is not known to be garbage;
* never into the repository's ``.gitignore`` — that would be an unasked change to the product;
* not on the founder's own machine (``runs_in_place``) — their checkout is theirs;
* undone for a path the agent writes later (``keep``), so a real file in the same place counts.

Best-effort throughout: a tree that is not a repository, or a git query that fails, leaves
everything as it was. Verification itself is never failed or delayed by this.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import structlog

from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

_GIT_TIMEOUT_S = 30.0
#: Marks the lines this module owns, so ``keep`` never removes a line someone else wrote.
_MARK = "# bsvibe: left by verification"
_GLOB_CHARS = ("\\", "*", "?", "[")


async def _sh(box: SandboxSession, command: str) -> tuple[int | None, str]:
    # S604: the sandbox Protocol's own flag; every interpolated value is quoted by the caller.
    res = await box.exec(  # noqa: S604
        f"cd {shlex.quote(box.workspace_mount)} && {command}",
        timeout_s=_GIT_TIMEOUT_S,
        shell=True,
    )
    return res.exit_code, res.stdout or ""


async def _untracked(box: SandboxSession) -> set[str] | None:
    """Untracked, not-ignored entries — a wholly untracked directory as ONE ``dir/`` entry."""
    code, out = await _sh(
        box, "git -c core.quotePath=false ls-files --others --exclude-standard --directory"
    )
    if code != 0:
        return None
    return {line for line in out.splitlines() if line.strip()}


def _pattern(entry: str) -> str:
    """``entry`` as an anchored, literal exclude pattern."""
    escaped = entry
    for ch in _GLOB_CHARS:
        escaped = escaped.replace(ch, "\\" + ch)
    return "/" + escaped


def _covers(pattern: str, path: str) -> bool:
    target = pattern[1:].replace("\\", "")
    return path == target or (target.endswith("/") and path.startswith(target))


async def _exclude_file(box: SandboxSession) -> str | None:
    code, out = await _sh(box, "git rev-parse --git-path info/exclude")
    return out.strip() if code == 0 and out.strip() else None


async def _release(box: SandboxSession, keep: Sequence[str]) -> None:
    """Drop this module's lines that cover a path the agent has since written."""
    path = await _exclude_file(box)
    if path is None:
        return
    code, text = await _sh(box, f"cat {shlex.quote(path)} 2>/dev/null || true")
    lines = text.splitlines()
    owned = {lines[i + 1] for i, line in enumerate(lines[:-1]) if line == _MARK}
    drop = {p for p in owned if any(_covers(p, k) for k in keep)}
    if not drop:
        return
    kept: list[str] = []
    for i, line in enumerate(lines):
        if line in drop or (line == _MARK and i + 1 < len(lines) and lines[i + 1] in drop):
            continue
        kept.append(line)
    body = "".join(f"{line}\n" for line in kept)
    await _sh(box, f"printf %s {shlex.quote(body)} > {shlex.quote(path)}")
    logger.info("verification_leftovers_released", paths=sorted(drop))


async def _exclude(box: SandboxSession, entries: set[str]) -> None:
    path = await _exclude_file(box)
    if path is None:
        return
    block = "".join(f"{_MARK}\n{_pattern(e)}\n" for e in sorted(entries))
    folder = path.rsplit("/", 1)[0] if "/" in path else "."
    await _sh(
        box,
        f"mkdir -p {shlex.quote(folder)} && printf %s {shlex.quote(block)} >> {shlex.quote(path)}",
    )
    logger.info(
        "verification_leftovers_excluded",
        count=len(entries),
        entries=sorted(entries)[:20],
    )


@asynccontextmanager
async def leftovers_excluded(
    box: SandboxSession, *, keep: Sequence[str] = ()
) -> AsyncIterator[None]:
    """Around verification: what it leaves behind is excluded from the run's commits."""
    if getattr(box, "runs_in_place", False):
        yield
        return
    before: set[str] | None = None
    try:
        if keep:
            await _release(box, keep)
        before = await _untracked(box)
    except Exception:  # noqa: BLE001 — never in verification's way
        logger.warning("verification_leftovers_snapshot_failed", exc_info=True)
    try:
        yield
    finally:
        if before is not None:
            try:
                after = await _untracked(box)
                if after:
                    new = after - before
                    if new:
                        await _exclude(box, new)
            except Exception:  # noqa: BLE001 — never in verification's way
                logger.warning("verification_leftovers_exclude_failed", exc_info=True)


__all__ = ["leftovers_excluded"]
