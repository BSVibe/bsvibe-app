"""Discover a repo's OWN acceptance gate — stack-agnostic.

BSVibe owns the verification *framework* (the invariants I1/I2/I3 + the honesty
ladder); it does NOT own "what is good". For a code repo the definition of done
is the repo's own gate — its CI / build / test — expressed in whatever language
and tooling the repo already uses. Hardcoding a stack-specific check list
(ruff/mypy/pytest) is a band-aid that only fits one stack and one kind of work.

This module is the stack-agnostic *discovery* half of invariant **I1**: given a
repo working tree, return the gate commands the repo already declares, in
priority order (the repo's real CI is the most authoritative source). It returns
an empty gate when the repo declares none — the caller then records the honesty
grade "no gate" rather than fabricating a strong "verified".

Pure + offline: reads files under ``repo_root`` only, runs nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

# Script/target names we treat as gate steps (a repo declaring these is telling
# us how it verifies itself). Order here is the order we emit within a source.
_GATE_STEP_NAMES: tuple[str, ...] = (
    "lint",
    "format",
    "fmt",
    "typecheck",
    "check",
    "build",
    "test",
)


@dataclass(frozen=True)
class GateCommand:
    """One command from the repo's declared gate, with provenance."""

    label: str
    command: str
    source: str


@dataclass(frozen=True)
class DiscoveredGate:
    """The repo's declared acceptance gate (empty ⇒ honesty grade 'no gate')."""

    origin: str
    commands: tuple[GateCommand, ...]

    @property
    def is_empty(self) -> bool:
        return not self.commands


def discover_gate(repo_root: Path) -> DiscoveredGate:
    """Return the repo's own declared gate, or an empty gate.

    Detectors run in priority order; the first that yields any command wins. The
    repo's real CI is the most authoritative source, so it is tried first — we
    run what the repo runs, not what BSVibe assumes.
    """
    for detector in _DETECTORS:
        gate = detector(repo_root)
        if gate is not None and gate.commands:
            return gate
    return DiscoveredGate(origin="none", commands=())


# ── Detectors ────────────────────────────────────────────────────────────────


def _detect_github_actions(root: Path) -> DiscoveredGate | None:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return None
    commands: list[GateCommand] = []
    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        commands.extend(_workflow_commands(wf, root))
    if not commands:
        return None
    return DiscoveredGate(origin="github-actions", commands=tuple(commands))


def _workflow_commands(wf: Path, root: Path) -> list[GateCommand]:
    rel = wf.relative_to(root).as_posix()
    try:
        doc = yaml.safe_load(wf.read_text())
    except (yaml.YAMLError, OSError):
        return []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    out: list[GateCommand] = []
    for job_name, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            out.extend(_step_commands(step, source=f"{rel}:{job_name}", job_name=job_name))
    return out


def _step_commands(step: object, *, source: str, job_name: object) -> list[GateCommand]:
    if not isinstance(step, dict):
        return []
    run = step.get("run")
    if not isinstance(run, str):
        return []
    name = step.get("name") if isinstance(step.get("name"), str) else None
    label = name or (str(job_name) if job_name is not None else "ci")
    return [GateCommand(label=label, command=line, source=source) for line in _split_run_block(run)]


def _detect_makefile(root: Path) -> DiscoveredGate | None:
    makefile = root / "Makefile"
    if not makefile.is_file():
        return None
    try:
        text = makefile.read_text()
    except OSError:
        return None
    targets = _makefile_targets(text)
    commands = [
        GateCommand(label=t, command=f"make {t}", source="Makefile")
        for t in _GATE_STEP_NAMES
        if t in targets
    ]
    if not commands:
        return None
    return DiscoveredGate(origin="makefile", commands=tuple(commands))


def _detect_package_json(root: Path) -> DiscoveredGate | None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    commands = [
        GateCommand(label=name, command=f"npm run {name}", source="package.json:scripts")
        for name in _GATE_STEP_NAMES
        if name in scripts
    ]
    if not commands:
        return None
    return DiscoveredGate(origin="package.json", commands=tuple(commands))


def _detect_cargo(root: Path) -> DiscoveredGate | None:
    if not (root / "Cargo.toml").is_file():
        return None
    # A Cargo manifest IS the declaration — these are the canonical Rust gate
    # commands, not a heuristic guess.
    commands = (
        GateCommand("fmt", "cargo fmt --check", "Cargo.toml"),
        GateCommand("lint", "cargo clippy -- -D warnings", "Cargo.toml"),
        GateCommand("build", "cargo build", "Cargo.toml"),
        GateCommand("test", "cargo test", "Cargo.toml"),
    )
    return DiscoveredGate(origin="cargo", commands=commands)


def _detect_go(root: Path) -> DiscoveredGate | None:
    if not (root / "go.mod").is_file():
        return None
    commands = (
        GateCommand("build", "go build ./...", "go.mod"),
        GateCommand("lint", "go vet ./...", "go.mod"),
        GateCommand("test", "go test ./...", "go.mod"),
    )
    return DiscoveredGate(origin="go", commands=commands)


_DETECTORS: tuple[Callable[[Path], DiscoveredGate | None], ...] = (
    _detect_github_actions,
    _detect_makefile,
    _detect_package_json,
    _detect_cargo,
    _detect_go,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _split_run_block(run: str) -> list[str]:
    """Split a workflow ``run:`` scalar into individual commands.

    A multi-line ``run: |`` block is a shell script; we treat each non-blank,
    non-comment line as a command. Trailing line-continuations (``\\``) join.
    """
    lines: list[str] = []
    buffer = ""
    for raw in run.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue
        buffer += stripped
        lines.append(buffer)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


_MAKE_TARGET_RE = re.compile(r"^([A-Za-z][\w-]*)\s*:(?!=)")


def _makefile_targets(text: str) -> set[str]:
    targets: set[str] = set()
    for line in text.splitlines():
        m = _MAKE_TARGET_RE.match(line)
        if m:
            targets.add(m.group(1))
    return targets
