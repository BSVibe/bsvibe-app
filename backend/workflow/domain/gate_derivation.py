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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

#: A derived command's role: a static QUALITY check (lint/format/type), a
#: behavioural TEST run, or a SURFACE check that drives the delivered behaviour
#: the way a user receives it. All three RUN the same way — the split is what
#: lets the proof surface say WHICH claim was earned, and those are different
#: claims: "this repo passed its own checks" is what CI already gives, while
#: "this change works where the user receives it" is the one that matters and
#: the one two live defects slipped past (a silent truncation that fabricated a
#: number; a dispatch that delivered nothing).
CommandKind = Literal["quality", "test", "surface"]


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
    kind = str(raw).strip().lower()
    if kind == "test":
        return "test"
    if kind == "surface":
        return "surface"
    return "quality"


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


def surface_exercised(commands: Sequence[Mapping[str, Any]]) -> bool:
    """Did a check actually drive the DELIVERED behaviour, and pass?

    Over the RECORDED command results (not the derived plan), because the plan
    says what was meant to run and only the record says what did.

    Both halves are load-bearing. A surface check that was not runnable on that
    machine (exit 127 → ``unavailable``) puts the strongest claim in the record
    on the weakest evidence there is — none — and one that RAN and failed is not
    an exercise either; it is a defect.
    """
    return any(c.get("kind") == "surface" and c.get("status") == "passed" for c in commands)


_DERIVATION_SYSTEM_PROMPT = (
    "You are an INDEPENDENT verification-gate deriver. Given a repository's OWN "
    "declarations and the files a work step changed, output the exact shell "
    "commands that verify the change USING THE REPO'S OWN TOOLCHAIN — the same "
    "way this project already checks itself. The repo may be in ANY language or "
    "build system; read its manifests to learn how IT runs its checks, and do "
    "not assume any particular ecosystem's conventions.\n"
    "GROUND every command in what the repo actually declares: use only tools, "
    "runners, flags, and extras that appear in the provided manifests / build "
    "config / CI. Never invent a flag, extra, or module invocation the repo does "
    "not define (e.g. do not add `--extra dev` unless a manifest declares that "
    "extra). Prefer a command the repo declares VERBATIM (a build-tool target, a "
    "package script, a CI step). When a manifest CONFIGURES a linter, type "
    "checker, or test framework — in its tool config or its dependencies — but "
    "states no explicit command, derive that tool's conventional invocation "
    "through the repo's own runner.\n"
    "PREFER STRONG checks that genuinely exercise the change: the repo's own test "
    "run for the changed code, plus its real lint / format / type checks. A mere "
    "syntax- or compile-only check (it only parses the file, proving almost "
    "nothing) is WEAK — do not return it as the gate when the repo's toolchain "
    "supports a real check. Emit a `kind:test` command whenever the change ships "
    "tests the repo can run.\n"
    "SCOPE quality checks to the CHANGED files, not the whole repo, so pre-existing "
    "debt in untouched files does not fail the change — this scoping does NOT apply "
    "to surface checks, which are about the delivered behaviour and are declared "
    "somewhere other than the changed files by their nature.\n"
    "SURFACE checks: if the repo DECLARES checks that drive its delivered behaviour "
    "end-to-end — the way a user receives it, rather than a unit of code — emit them "
    "with `kind:surface`. A declaration is a marked/named suite, a build target, or a "
    "script the repo itself defines for that purpose; its name or description usually "
    "says so. NEVER invent one: if the repo declares no such check, emit none, because "
    "a fabricated end-to-end command proves nothing and fails for the wrong reason.\n"
    "CONSTRAINTS: the task intent often states not only what to build but what NOT "
    "to do. Every such constraint is verifiable and must become its own command "
    "whose EXIT CODE decides whether it held — exit 0 when the constraint was "
    "respected, non-zero when it was violated. Read the intent for them; do not "
    "expect a fixed set, since they are stated in ordinary language and are "
    "therefore unbounded. When a baseline commit is given, anchor a constraint about what changed to THAT ref rather than to the working tree — the agent "
    "commits as it works, so a check that only inspects uncommitted changes sees "
    "almost nothing. Emit these as `kind:quality`.\n"
    "If the change is not something a command can verify (pure prose / design / a "
    'doc) AND the intent states no constraint, set "applicable" to false and return '
    "no commands — that is a valid, honest answer; the judge and demonstration paths "
    "cover it. A stated constraint is checkable even when the work produced nothing "
    'to run: "produced no output" is not the same as "has nothing to prove", and '
    "whether the constraint held is exactly what is left to prove. An intent carrying "
    "one is therefore applicable.\n"
    'Output ONLY a JSON object: {"applicable": bool, "commands": [ {"command": '
    'str, "kind": "quality"|"test"|"surface", "rationale": str} ]}. No prose.'
)


def derivation_planner_messages(
    *,
    manifests: dict[str, str],
    changed_files: list[str],
    intent: str,
    baseline: str | None = None,
) -> list[dict[str, str]]:
    """Build the (system, user) message pair grounding the deriver in the repo.

    ``manifests`` maps a repo-relative path (pyproject.toml, package.json,
    Cargo.toml, Makefile, a CI workflow, …) to its content — ONLY the files that
    actually exist, so the LLM cannot ground on a manifest the repo lacks.

    ``baseline`` is where the tree stood before the run touched it. Offered so a
    constraint check can be anchored to it; OMITTED entirely when unknown rather
    than invented, since a check against a ref that does not exist fails for the
    wrong reason."""
    manifest_block = (
        "\n\n".join(f"=== {path} ===\n{content}" for path, content in manifests.items())
        if manifests
        else "(no manifests / build config found in this repo)"
    )
    changed_block = "\n".join(changed_files) if changed_files else "(no files changed)"
    baseline_block = (
        f"Baseline commit (where the tree stood before this run):\n{baseline}\n\n"
        if baseline
        else ""
    )
    user = (
        f"Task intent:\n{intent}\n\n"
        f"{baseline_block}"
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
    "surface_exercised",
]
