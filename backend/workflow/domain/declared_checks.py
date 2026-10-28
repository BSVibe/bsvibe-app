"""Checks a PRODUCT declares for its own verification — and the CI conditions they run under.

The derived gate is written by a model, fresh each round. That is right for "how do I check
THIS change", and wrong for what a product needs checked every time: its repo-wide guards, the
suite its CI runs before merge. A check a model must remember to include is a check that is
sometimes missing — prod #1238/#1240 passed verification and then turned CI red.

So a product names them, beside ``verify_stack`` and ``verify_secrets``:

* ``verify_floor`` — commands run on EVERY verification, beside the derived ones. Fast,
  always-relevant guards.
* ``verify_final`` — commands run ONCE, after every other check passed: the CI-equivalent full
  run, so a change that would fail CI fails inside the round instead of after delivery. Not
  spent on a round that has already failed — that round's work is about to change.

And every gate command runs with ``CI=true``. CI sets it, so the repo's tools behave the CI way
there (pnpm's non-interactive install, frozen lockfiles, test runners without watch mode). A
sandbox without it ran the same command as a different command — prod #1204 failed 8 rounds on
``ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY``, which no change to the work could fix.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FLOOR_KEY = "verify_floor"
FINAL_KEY = "verify_final"
#: Fast checks run when the agent says it is done, BEFORE a verification round is spent: a
#: failure goes back as a turn (prod #1259 spent a 16-minute round on a one-line type error).
PRECHECK_KEY = "verify_precheck"
_KEYS = (FLOOR_KEY, FINAL_KEY, PRECHECK_KEY)

#: CI's own marker. In the command string, not the exec ``env``: some boxes run the command
#: one shell removed (a disposable environment wraps it in ``docker exec``), where an outer
#: environment variable never reaches it. It is not a secret, so the command string is fine.
CI_PREFIX = "export CI=true; "


@dataclass(frozen=True)
class DeclaredChecks:
    floor: tuple[str, ...] = ()
    final: tuple[str, ...] = ()
    precheck: tuple[str, ...] = ()


def _commands(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(c.strip() for c in raw if isinstance(c, str) and c.strip())


def read_declared_checks(metadata: Mapping[str, Any] | None) -> DeclaredChecks:
    """The product's declared checks. Lenient: the write seam already refused bad shapes."""
    meta = metadata or {}
    return DeclaredChecks(
        floor=_commands(meta.get(FLOOR_KEY)),
        final=_commands(meta.get(FINAL_KEY)),
        precheck=_commands(meta.get(PRECHECK_KEY)),
    )


def validate_declared_checks(metadata: Mapping[str, Any]) -> None:
    """Refuse a declaration that would be silently read as nothing.

    ``"uv run pytest"`` instead of ``["uv run pytest"]`` would read as an empty list — a founder
    who declared a floor would be told nothing and get none."""
    for key in _KEYS:
        if key not in metadata or metadata[key] is None:
            continue
        raw = metadata[key]
        if not isinstance(raw, list) or not all(isinstance(c, str) and c.strip() for c in raw):
            raise ValueError(f"{key} must be a list of non-empty shell commands")


def gate_command(command: str, venv_bin: str | None) -> str:
    """``command`` as a gate runs it: under CI's conditions, inside the project venv if any.

    The RECORDED command stays ``command`` — these are execution details, not what was
    verified."""
    path = f'export PATH="{venv_bin}:$PATH"; ' if venv_bin else ""
    return f"{CI_PREFIX}{path}{command}"


__all__ = [
    "CI_PREFIX",
    "DeclaredChecks",
    "FINAL_KEY",
    "FLOOR_KEY",
    "PRECHECK_KEY",
    "gate_command",
    "read_declared_checks",
    "validate_declared_checks",
]
