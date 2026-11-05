"""Ledger items that ask for COMMANDS to pass (pure domain).

A request item such as "`npx --yes pnpm@9 lint`, `… exec tsc --noEmit`, `… test` 통과" is not a
claim an agent can evidence and a judge can read: it is a set of commands, and their exit codes
are the proof. The gate therefore runs them itself and decides the item from the results.

The request does not say WHERE or HOW this repo runs them (the PWA lives in ``apps/pwa``; the
repo root has no ``package.json``), so the string is never executed as written. The deriver is
handed the commands and returns the repo's own runnable form of each, tagged with the ledger
command it covers; the item is decided from those results. A command nobody covered is not run
and not guessed: the item is left ``unclear`` with the reason.

The issue text abbreviates: ``…`` stands for the prefix of the first command. It is expanded
only when that prefix is certain: the first command must be a package-script invocation
(``npx --yes pnpm@9 lint``) whose last word is a script name, so the prefix is everything before
it. ``uv run pytest tests/x.py`` ends in a path and ``uv run pytest`` in a tool, not a
sub-command — there ``… ruff check`` stays unresolved and the item ``unclear``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from backend.workflow.domain.requirements import OWED_KINDS

_SPAN = re.compile(r"`([^`\n]+)`")
_PASS_WORD = re.compile(r"통과|\bpass(?:es|ed)?\b|\bsucceed", re.IGNORECASE)
_ELLIPSIS = re.compile(r"^(?:…|\.{3})\s*")
#: Package-script runners: ``<runner> … <script>``, where the last word names a script.
_SCRIPT_RUNNERS = frozenset({"npx", "pnpm", "npm", "yarn", "bun", "bunx"})
_SCRIPTS = frozenset(
    {"lint", "test", "build", "typecheck", "format", "check", "dev", "start", "e2e", "test:e2e"}
)
_WORD = re.compile(r"^[a-z][\w.@-]*$")
#: A lone word is a command only when it is a tool a repo runs by name; ``foo.py`` is a file.
_BARE_RUNNERS = frozenset(
    {"pytest", "make", "tox", "nox", "ruff", "mypy", "pyright", "tsc", "eslint", "vitest", "jest"}
)


@dataclass(frozen=True)
class LedgerCommands:
    """The commands one ledger item requires to pass. ``unresolved`` lists abbreviated spans
    that could not be expanded, verbatim."""

    commands: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


def _is_command(span: str) -> bool:
    tokens = span.split()
    if not tokens or not _WORD.match(tokens[0]):
        return False
    return len(tokens) > 1 or tokens[0] in _BARE_RUNNERS


def _script_prefix(tokens: list[str]) -> list[str] | None:
    """The prefix ``…`` stands for, or ``None`` when it is not certain."""
    if len(tokens) < 2 or tokens[-1] not in _SCRIPTS:
        return None
    if not any(t.split("@")[0] in _SCRIPT_RUNNERS for t in tokens[:-1]):
        return None
    return tokens[:-1]


def commands_required(text: str) -> LedgerCommands | None:
    """The commands an item requires to pass; ``None`` when it does not ask for that."""
    if not _PASS_WORD.search(text):
        return None
    commands: list[str] = []
    unresolved: list[str] = []
    base: list[str] | None = None
    for raw in (m.group(1).strip() for m in _SPAN.finditer(text)):
        abbreviated = _ELLIPSIS.match(raw)
        if abbreviated:
            rest = raw[abbreviated.end() :].strip()
            if base and rest:
                command = " ".join([*base, rest])
                if command not in commands:
                    commands.append(command)
            else:
                unresolved.append(raw)
        elif _is_command(raw):
            if not commands:  # only the first command can lend its prefix
                base = _script_prefix(raw.split())
            if raw not in commands:
                commands.append(raw)
    if not commands and not unresolved:
        return None
    return LedgerCommands(tuple(commands), tuple(unresolved))


def ledger_command_plan(requirements: Sequence[Mapping[str, object]]) -> dict[str, LedgerCommands]:
    """Item id → the commands that item requires, for the live owed items that ask for any."""
    plan: dict[str, LedgerCommands] = {}
    for item in requirements:
        if item.get("withdrawn") or item.get("kind", "do") not in OWED_KINDS:
            continue
        required = commands_required(str(item.get("text") or ""))
        if required is not None:
            plan[str(item["id"])] = required
    return plan


def plan_commands(plan: Mapping[str, LedgerCommands]) -> tuple[str, ...]:
    """Every distinct command the plan needs run, in order."""
    out: list[str] = []
    for required in plan.values():
        out.extend(c for c in required.commands if c not in out)
    return tuple(out)


def command_verdict(
    required: LedgerCommands, results: Sequence[Mapping[str, object]]
) -> tuple[str, str]:
    """``(status, note)`` for an item, from the exit codes of its commands alone.

    Any command that ran and failed → ``rejected`` naming it; otherwise a command that did not
    run (tool missing, never executed) or could not be expanded → ``unclear`` with the reason;
    otherwise ``done``."""
    # A result counts for a ledger command only when the deriver tagged it as covering it.
    by_command: dict[str, Mapping[str, object]] = {}
    for r in results:
        covers = r.get("covers")
        for ledger_command in covers if isinstance(covers, list | tuple) else ():
            prior = by_command.get(str(ledger_command))
            if prior is None or prior.get("status") == "passed":
                by_command[str(ledger_command)] = r
    failed: list[str] = []
    not_run: list[str] = []
    for command in required.commands:
        status = (by_command.get(command) or {}).get("status")
        if status == "passed":
            continue
        (failed if status == "failed" else not_run).append(command)
    if failed:
        return "rejected", "실패한 명령: " + ", ".join(f"`{c}`" for c in failed)
    reasons: list[str] = []
    if not_run:
        reasons.append(
            "리포에 맞는 실행 형태가 없거나 실행되지 않음: " + ", ".join(f"`{c}`" for c in not_run)
        )
    if required.unresolved:
        reasons.append(
            "축약(…)을 펼칠 앞 명령이 없어 명령을 만들 수 없음: "
            + ", ".join(f"`{c}`" for c in required.unresolved)
        )
    if reasons:
        return "unclear", "; ".join(reasons)
    return "done", "명령 통과: " + ", ".join(f"`{c}`" for c in required.commands)


def command_verdicts(
    plan: Mapping[str, LedgerCommands], results: Sequence[Mapping[str, object]]
) -> dict[str, tuple[str, str]]:
    return {item_id: command_verdict(required, results) for item_id, required in plan.items()}


__all__ = [
    "LedgerCommands",
    "command_verdict",
    "command_verdicts",
    "commands_required",
    "ledger_command_plan",
    "plan_commands",
]
