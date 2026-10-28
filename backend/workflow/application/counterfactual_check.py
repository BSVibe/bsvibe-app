"""Run each guard's cited test with the change taken away — the sandbox half.

In place, and only on a CLEAN tree: verification has committed the run's work, so ``HEAD`` is
the truth to restore to. The run's non-test changes are put back to the baseline (new files
removed), each cited test is run alone, and ``git checkout HEAD -- <paths>`` puts every path
back. A tree that is not clean is never touched — there would be nothing safe to restore to.

In place rather than in a second worktree on purpose: a copy does not carry the project's
environment (a venv whose editable install points at the ORIGINAL tree would test the change
anyway), and a check that silently tests the wrong code is worse than none.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import structlog

from backend.config import get_settings
from backend.workflow.application.gate_commands import run_gate_commands
from backend.workflow.domain.counterfactual import (
    COUNTERFACTUAL_KEY,
    UNKNOWN,
    guard_candidates,
    is_test_path,
    parse_single_test_command,
    single_test_messages,
    verdict,
)
from backend.workflow.domain.requirements import GateFacts, _is_test_run
from backend.workflow.infrastructure.sandbox import SandboxSession

logger = structlog.get_logger(__name__)

Complete = Callable[[list[dict[str, str]]], Awaitable[str]]

_GIT_TIMEOUT_S = 30.0
#: A change touching more non-test files than this is not taken away — the paths go on one
#: command line, and a change that large is not one a single guard test speaks to anyway.
_MAX_PATHS = 200
_NOTES = {
    "holds": "fails without the change",
    "vacuous": "passes without the change — this test does not guard what the change added",
}


def _mark(row: dict[str, Any], value: str, note: str) -> None:
    row[COUNTERFACTUAL_KEY] = value
    row["counterfactual_note"] = note


async def _git(box: SandboxSession, args: str) -> tuple[int | None, str]:
    # S604: the sandbox Protocol's own flag; every argument is quoted by the caller.
    res = await box.exec(  # noqa: S604
        f"git -C {shlex.quote(box.workspace_mount)} {args}", timeout_s=_GIT_TIMEOUT_S, shell=True
    )
    return res.exit_code, res.stdout or ""


def _reuse_prior(
    candidates: list[dict[str, Any]], prior_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Copy a verdict whose evidence is unchanged; return the rows still to check."""
    prior = {
        (str(r.get("id")), str(r.get("excerpt_hash"))): r
        for r in prior_rows
        if r.get(COUNTERFACTUAL_KEY) and r.get("excerpt_hash")
    }
    todo: list[dict[str, Any]] = []
    for row in candidates:
        cached = prior.get((str(row["id"]), str(row.get("excerpt_hash"))))
        if cached is None:
            todo.append(row)
        else:
            _mark(row, str(cached[COUNTERFACTUAL_KEY]), str(cached.get("counterfactual_note", "")))
    return todo


async def _commands_for(
    todo: list[dict[str, Any]], facts: GateFacts, complete: Complete
) -> dict[str, str]:
    """One command per row that runs its first cited test alone — rows without one are marked."""
    covering = [c for c in facts.passed_commands if _is_test_run(c)][:5]
    commands: dict[str, str] = {}
    for row in todo:
        test = str(row["tests"][0])
        try:
            command = parse_single_test_command(
                await complete(single_test_messages(test, covering)), test
            )
        except Exception:  # noqa: BLE001 — no command means not checked, never a verdict
            command = None
        if command is None:
            _mark(row, UNKNOWN, "no command that runs this test alone")
        else:
            commands[str(row["id"])] = command
    return commands


@asynccontextmanager
async def _without_change(
    box: SandboxSession, baseline: str, changed: list[str]
) -> AsyncIterator[bool]:
    """The tree with ``changed`` put back to ``baseline`` — and back to ``HEAD`` afterwards.

    Yields ``False`` (and touches nothing) when the tree is not clean or the baseline cannot
    be read: there would be nothing safe to restore to."""
    quoted = " ".join(shlex.quote(p) for p in changed)
    code, dirty = await _git(box, f"status --porcelain -- {quoted}")
    listed, at_baseline = await _git(
        box, f"ls-tree -r --name-only {shlex.quote(baseline)} -- {quoted}"
    )
    if code != 0 or dirty.strip() or listed != 0:
        yield False
        return
    existed = set(at_baseline.split())
    old = " ".join(shlex.quote(p) for p in changed if p in existed)
    new = " ".join(shlex.quote(p) for p in changed if p not in existed)
    try:
        if old:
            await _git(box, f"checkout {shlex.quote(baseline)} -- {old}")
        if new:
            await _git(box, f"rm -q --cached --ignore-unmatch -- {new}")
            await box.exec(  # noqa: S604 — the sandbox Protocol's flag; paths are quoted
                f"cd {shlex.quote(box.workspace_mount)} && rm -f -- {new}",
                timeout_s=_GIT_TIMEOUT_S,
                shell=True,
            )
        yield True
    finally:
        await _git(box, f"checkout HEAD -- {quoted}")
        code, left = await _git(box, f"status --porcelain -- {quoted}")
        if code != 0 or left.strip():
            logger.error("counterfactual_restore_incomplete", paths=changed, left=left[:500])


async def _committed_changes(box: SandboxSession, baseline: str) -> list[str] | None:
    """Non-test paths the run's COMMITS changed — what ``HEAD`` can put back. Never the
    working tree's untracked files: a tool's leftovers are not the change (prod #1259)."""
    code, out = await _git(box, f"diff --name-only {shlex.quote(baseline)} HEAD")
    if code != 0:
        return None
    return sorted(p for p in out.split() if p and not is_test_path(p))


async def check_guards(
    rows: list[dict[str, Any]],
    *,
    box: SandboxSession,
    baseline: str,
    facts: GateFacts,
    complete: Complete,
    venv_bin: str | None,
    prior_rows: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Annotate each checkable guard row with ``counterfactual``. Never changes a status, and
    never raises: report-only work that ends a run would cost more than it reports (#1259)."""
    candidates = [r for r in rows if any(r is c for c in guard_candidates(rows))]
    try:
        await _check(candidates, box, baseline, facts, complete, venv_bin, prior_rows)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.warning("counterfactual_failed", error_type=type(exc).__name__, exc_info=True)
        for row in candidates:
            if not row.get(COUNTERFACTUAL_KEY):
                _mark(row, UNKNOWN, "the check could not run")


async def _check(
    candidates: list[dict[str, Any]],
    box: SandboxSession,
    baseline: str,
    facts: GateFacts,
    complete: Complete,
    venv_bin: str | None,
    prior_rows: Sequence[Mapping[str, Any]],
) -> None:
    todo = _reuse_prior(candidates, prior_rows)
    if not todo:
        return
    changed = await _committed_changes(box, baseline)
    if not changed or len(changed) > _MAX_PATHS:
        reason = (
            "the run changed only tests — nothing to take away"
            if changed == []
            else "the change could not be listed, or is too large to take away"
        )
        logger.info("counterfactual_skipped", paths=None if changed is None else len(changed))
        for row in todo:
            _mark(row, UNKNOWN, reason)
        return
    commands = await _commands_for(todo, facts, complete)
    if not commands:
        return
    async with _without_change(box, baseline, changed) as ready:
        for row in todo:
            command = commands.get(str(row["id"]))
            if command is None:
                continue
            if not ready:
                _mark(row, UNKNOWN, "the tree was not clean, so the change could not be taken away")
                continue
            [result] = await run_gate_commands(
                box,
                [(command, "counterfactual")],
                venv_bin=venv_bin,
                timeout_s=get_settings().verify_command_timeout_s,
            )
            value = verdict(result.get("exit_code"), bool(result.get("timed_out")))
            _mark(row, value, _NOTES.get(value, "the test could not run without the change"))
    logger.info(
        "counterfactual_checked",
        results={str(r["id"]): r.get(COUNTERFACTUAL_KEY) for r in candidates},
    )


__all__ = ["check_guards"]
