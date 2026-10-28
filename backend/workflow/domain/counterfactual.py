"""Does a guard test still pass with the change taken away? — the pure half.

A test cited for a guard item proves the guard only if it FAILS when the run's non-test changes
are put back to the baseline. One that still passes guards nothing the change introduced (a 404
test for a route that does not exist yet passes before the route, too).

Report-only (founder 2026-10-28): the finding is shown on the ledger row, never folded into the
verdict — a regression test for behaviour that already existed legitimately passes without the
change, and failing those would cost first-round passes for no gain.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: The ledger-row key, and its values.
COUNTERFACTUAL_KEY = "counterfactual"
HOLDS = "holds"  # fails without the change — a real guard
VACUOUS = "vacuous"  # passes without the change
UNKNOWN = "unknown"  # could not be checked (no command, a dirty tree, a tool fault)

#: At most this many guard items are checked per verification.
MAX_GUARDS = 5

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_FILE = re.compile(
    r"(^test_[^/]+$)|(_test\.[A-Za-z0-9]+$)|(\.(test|spec)\.[A-Za-z0-9]+$)|(^conftest\.py$)"
)

_SYSTEM = (
    "You write ONE shell command that runs exactly ONE test and nothing else. You are given the "
    "test's id and the commands that already ran it successfully as part of a larger run. Use "
    "the same runner, working directory and flags as those commands, narrowed to this one test "
    "(its file, and its name when the runner can select one). Answer with ONE JSON object: "
    '{"command": "<shell command>"}. No prose.'
)


def is_test_path(path: str) -> bool:
    """A test file by the conventions most stacks share — a test directory or a test name."""
    parts = path.strip("/").split("/")
    return any(p in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_FILE.search(parts[-1]))


def guard_candidates(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Guard items judged done on cited tests — the only ones a counterfactual can speak to."""
    return [
        r for r in rows if r.get("kind") == "guard" and r.get("status") == "done" and r.get("tests")
    ][:MAX_GUARDS]


def single_test_messages(test: str, covering: Sequence[str]) -> list[dict[str, str]]:
    ran = "\n".join(f"- {c}" for c in covering) or "(none recorded)"
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Test id: {test}\n\nCommands that ran it:\n{ran}"},
    ]


def parse_single_test_command(raw: str, test: str) -> str | None:
    """The model's command — only when it names the test's file (else it may run anything)."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1]) if start != -1 and end > start else None
    except ValueError:
        return None
    command = str(data.get("command") or "").strip() if isinstance(data, dict) else ""
    test_file = test.partition("::")[0].strip()
    return command if command and test_file and test_file in command else None


def verdict(exit_code: int | None, timed_out: bool) -> str:
    """Exit 0 without the change → vacuous; a real failure → holds; anything else → unknown."""
    if timed_out or exit_code is None or exit_code == 127:
        return UNKNOWN
    return VACUOUS if exit_code == 0 else HOLDS


__all__ = [
    "COUNTERFACTUAL_KEY",
    "HOLDS",
    "MAX_GUARDS",
    "UNKNOWN",
    "VACUOUS",
    "guard_candidates",
    "is_test_path",
    "parse_single_test_command",
    "single_test_messages",
    "verdict",
]
