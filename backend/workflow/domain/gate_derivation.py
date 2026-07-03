"""LLM-derived, repo-grounded verification gate (pure domain).

The verifier's "what commands prove THIS repo's work is correct" question is
answered by reading the repo's OWN declarations (its manifests / build config /
CI) rather than a per-stack detector list or a hardcoded ``uv run ruff`` quality
bar. One mechanism generalises across any stack — Python, Rust, Go, Node, or a
build system we've never seen — because the derivation is grounded in whatever
the repo actually declares, and the derived commands then RUN deterministically
(exit code is the verdict; a missing tool is ``unavailable``, never a false-fail).

This module is the PURE half — the tolerant parser of the LLM's output and the
grounding prompt builder. The LLM call + sandbox execution live in the service
layer (mirrors :mod:`backend.workflow.domain.outcome_demonstration`). Keeping it
pure keeps the domain free of an LLM dependency and makes the parsing +
prompt-shape independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: A derived command's role: a static QUALITY check (lint/format/type) or a
#: behavioural TEST run. Advisory only — both RUN the same way; the split lets
#: the proof surface group them and the honesty grade weight "tests ran".
CommandKind = Literal["quality", "test"]


@dataclass(frozen=True)
class DerivedCommand:
    """One repo-native verification command the LLM derived from the repo's own
    declarations (e.g. ``uv run ruff check foo.py`` for a repo whose pyproject
    configures ruff, ``cargo test`` for a Cargo manifest). Runs in the sandbox;
    exit 0 = pass, exit 127 = the tool isn't here (unavailable), else fail."""

    command: str
    kind: CommandKind = "quality"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "kind": self.kind, "rationale": self.rationale}


@dataclass(frozen=True)
class DerivedGate:
    """The verification gate derived for one work step.

    ``applicable`` — whether a runnable gate concept applies at all: True for a
    code change in a repo with a real toolchain, False for a pure-prose / design
    / non-code deliverable that no command can verify (that rides the judge +
    demonstration paths instead). Distinct from ``is_empty``: an applicable repo
    whose commands could not be derived is applicable-but-empty (weak evidence,
    not "not a code project"). A shape we cannot read at all is not-applicable —
    an honest downgrade, never a spurious runnable gate."""

    commands: tuple[DerivedCommand, ...] = ()
    applicable: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.commands

    def to_dict(self) -> dict[str, Any]:
        return {"applicable": self.applicable, "commands": [c.to_dict() for c in self.commands]}


def _coerce_kind(raw: Any) -> CommandKind:
    return "test" if str(raw).strip().lower() == "test" else "quality"


def parse_derived_gate(raw: Any) -> DerivedGate:
    """Parse the LLM's derivation output tolerantly. Shape:
    ``{"applicable": bool, "commands": [{"command"|"cmd", "kind", "rationale"}]}``.

    A shape we cannot read at all → not-applicable + empty (honest downgrade).
    Empty / missing ``command`` entries are dropped; ``kind`` defaults to and any
    unknown value coerces to ``quality``; identical commands dedupe."""
    if not isinstance(raw, dict):
        return DerivedGate(applicable=False)
    raw_commands = raw.get("commands")
    if not isinstance(raw_commands, list):
        # The object carries no readable command list — we cannot tell this is a
        # code project with a gate, so default to NOT-applicable (honest
        # downgrade) unless the LLM explicitly asserted applicability.
        return DerivedGate(applicable=bool(raw.get("applicable", False)), commands=())
    commands: list[DerivedCommand] = []
    seen: set[str] = set()
    for item in raw_commands:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or item.get("cmd") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        commands.append(
            DerivedCommand(
                command=command,
                kind=_coerce_kind(item.get("kind")),
                rationale=str(item.get("rationale") or "").strip(),
            )
        )
    return DerivedGate(
        commands=tuple(commands),
        applicable=bool(raw.get("applicable", True)),
    )


_DERIVATION_SYSTEM_PROMPT = (
    "You are an INDEPENDENT verification-gate deriver. Given a repository's OWN "
    "declarations and the files a work step changed, output the exact shell "
    "commands that verify the change USING THE REPO'S OWN TOOLCHAIN — the same "
    "way this project already checks itself.\n"
    "GROUND every command in what the repo actually declares: use only tools, "
    "runners, flags, and extras that appear in the provided manifests / build "
    "config / CI. Do NOT invent an extra, a module invocation, or a flag the "
    "repo does not define (e.g. do not add `--extra dev` unless a manifest "
    "declares a `dev` extra; do not run `python -m ruff` if the project runs its "
    "tools through a runner like `uv run`). Prefer the project's declared "
    "commands verbatim (a Makefile target, a package script, a CI step, `cargo "
    "test`, `go test ./...`).\n"
    "SCOPE quality checks to the CHANGED files, not the whole repo, so pre-existing "
    "debt in untouched files does not fail the change.\n"
    "If the change is not code a command can verify (pure prose / design / a doc), "
    'set "applicable" to false and return no commands — that is a valid, honest '
    "answer; the judge and demonstration paths cover it.\n"
    'Output ONLY a JSON object: {"applicable": bool, "commands": [ {"command": '
    'str, "kind": "quality"|"test", "rationale": str} ]}. No prose.'
)


def derivation_planner_messages(
    *,
    manifests: dict[str, str],
    changed_files: list[str],
    intent: str,
) -> list[dict[str, str]]:
    """Build the (system, user) message pair grounding the deriver in the repo.

    ``manifests`` maps a repo-relative path (pyproject.toml, package.json,
    Cargo.toml, Makefile, a CI workflow, …) to its content — ONLY the files that
    actually exist, so the LLM cannot ground on a manifest the repo lacks."""
    manifest_block = (
        "\n\n".join(f"=== {path} ===\n{content}" for path, content in manifests.items())
        if manifests
        else "(no manifests / build config found in this repo)"
    )
    changed_block = "\n".join(changed_files) if changed_files else "(no files changed)"
    user = (
        f"Task intent:\n{intent}\n\n"
        f"Files changed by this work step:\n{changed_block}\n\n"
        f"The repository's own declarations:\n{manifest_block}"
    )
    return [
        {"role": "system", "content": _DERIVATION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = [
    "CommandKind",
    "DerivedCommand",
    "DerivedGate",
    "derivation_planner_messages",
    "parse_derived_gate",
]
