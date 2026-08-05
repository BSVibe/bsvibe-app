"""Scaffold a minimal acceptance gate for a project that declares none.

Invariant **I1** needs the target's OWN gate to run against (see
:mod:`backend.workflow.domain.gate_discovery`). But a project BSVibe starts —
or a cloned repo that never set up CI — may declare no gate at all, so
:func:`~backend.workflow.domain.gate_discovery.discover_gate` returns empty and
"verified" can only ever earn the weakest honesty grade (no gate).

The founder decision (2026-07-01): at bootstrap, BSVibe SCAFFOLDS a real gate —
a minimal CI (lint + test + build) for the detected stack — so the project owns
a visible, runnable definition of done that also runs at PR time on GitHub, and
:func:`discover_gate` has something to parse on the next run.

This module is the pure, offline half: detect the stack from its manifest and
return the gate file to write (path + content). It reads ``repo_root`` only and
writes NOTHING — the bootstrap runtime does the write + commit. It NEVER
clobbers: if the repo already declares a gate (``discover_gate`` non-empty) or a
``ci.yml`` already exists, it returns ``None``.

Note the interaction with ``discover_gate``'s detectors: a Cargo / go.mod /
package.json-with-scripts repo is already NON-empty, so scaffolding is a no-op
for it (it keeps its own gate). In practice the gap this fills is **Python**
(``discover_gate`` has no pyproject detector) and a **node** repo whose
``package.json`` declares no lint/test/build script.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.workflow.domain.gate_discovery import discover_gate

#: Where the scaffolded gate is written (a standard GitHub Actions workflow so
#: ``discover_gate``'s github-actions detector — the most authoritative source —
#: parses it back).
SCAFFOLD_REL_PATH = ".github/workflows/ci.yml"

#: Manifest → stack, in the order we probe. First match wins.
_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
)

# ── CI templates ─────────────────────────────────────────────────────────────
# Each is a valid GitHub Actions workflow (real CI at PR time) whose static
# steps (lint / format) are exactly what the verify sandbox can run in isolation
# (L-I1b runs the source-deterministic ``run:`` steps and defers install + test
# runners). Keep the ``run:`` steps as bare tool invocations so the sandbox
# resolves them against its own toolchain when the project has no venv.

# Python is generated per-repo (see ``_python_ci``): a Python project declares
# its test tooling (pytest/ruff) in a dev/test extra or a uv dependency-group,
# NOT in the base install (#689). A static ``pip install -e . ruff`` + bare
# ``pytest`` yields a gate NO correct code can pass — the tools are never
# installed. The generated CI reflects what the repo actually declares.
_DEV_EXTRA_NAMES = frozenset({"dev", "test", "tests", "testing"})

_NODE_CI = """\
name: CI
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Install
        run: npm ci
      - name: Lint
        run: npm run lint --if-present
      - name: Test
        run: npm test --if-present
"""

_GO_CI = """\
name: CI
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"
      - name: Vet
        run: go vet ./...
      - name: Build
        run: go build ./...
      - name: Test
        run: go test ./...
"""

_RUST_CI = """\
name: CI
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Format
        run: cargo fmt --check
      - name: Lint
        run: cargo clippy -- -D warnings
      - name: Build
        run: cargo build
      - name: Test
        run: cargo test
"""

#: Static templates for stacks whose gate does not depend on per-repo dependency
#: declarations. Python is generated dynamically (see :func:`_python_ci`).
_TEMPLATES: dict[str, str] = {
    "node": _NODE_CI,
    "go": _GO_CI,
    "rust": _RUST_CI,
}


def _declared_dependencies(project: dict[str, Any], groups: dict[str, Any]) -> list[str]:
    """Flatten every dependency string the pyproject declares — base deps,
    every optional-dependencies extra, and every PEP 735 dependency-group."""
    declared: list[str] = []
    base = project.get("dependencies")
    if isinstance(base, list):
        declared += [str(d) for d in base]
    for table in (project.get("optional-dependencies"), groups):
        if isinstance(table, dict):
            for vals in table.values():
                if isinstance(vals, list):
                    declared += [str(d) for d in vals if isinstance(d, str)]
    return declared


def _python_ci(repo_root: Path) -> str:
    """Generate a Python CI workflow that installs the repo's OWN test deps.

    Grounds every step in what ``pyproject.toml`` declares (#689):

    * ``uv.lock`` present → sync + run the tools through uv (``uv sync`` pulls
      the extras + dev groups, so pytest/ruff are actually installed);
    * else a dev/test extra declared → ``pip install -e ".[<extra>]"`` so the
      Test step's ``pytest`` exists;
    * a Test step is emitted ONLY when the repo declares a test runner — a
      ``pytest`` step in a project with no tests is unpassable, so we drop it
      rather than manufacture an impossible check.
    """
    raw = ""
    data: dict[str, Any] = {}
    try:
        raw = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    project = data.get("project") if isinstance(data, dict) else None
    project = project if isinstance(project, dict) else {}
    groups = data.get("dependency-groups") if isinstance(data, dict) else None
    groups = groups if isinstance(groups, dict) else {}

    declared = _declared_dependencies(project, groups)
    has_pytest = (
        any("pytest" in d for d in declared)
        or "[tool.pytest" in raw
        or (repo_root / "tests").is_dir()
    )
    optional = project.get("optional-dependencies")
    dev_extra: str | None = None
    if isinstance(optional, dict):
        dev_extra = next((str(k) for k in optional if str(k).lower() in _DEV_EXTRA_NAMES), None)

    steps = ["      - uses: actions/checkout@v4"]
    if (repo_root / "uv.lock").is_file():
        steps.append("      - uses: astral-sh/setup-uv@v5")
        steps.append("      - name: Install\n        run: uv sync --all-extras")
        steps.append("      - name: Lint\n        run: uv run ruff check .")
        steps.append("      - name: Format\n        run: uv run ruff format --check .")
        if has_pytest:
            steps.append("      - name: Test\n        run: uv run pytest")
    else:
        steps.append(
            '      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.11"'
        )
        install = f'pip install -e ".[{dev_extra}]" ruff' if dev_extra else "pip install -e . ruff"
        steps.append(f"      - name: Install\n        run: {install}")
        steps.append("      - name: Lint\n        run: ruff check .")
        steps.append("      - name: Format\n        run: ruff format --check .")
        if has_pytest:
            steps.append("      - name: Test\n        run: pytest")

    body = "\n".join(steps)
    return (
        "name: CI\n"
        "on: [push, pull_request]\n"
        "jobs:\n"
        "  ci:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        f"{body}\n"
    )


@dataclass(frozen=True)
class ScaffoldedGate:
    """A gate file to write into the repo (path relative to ``repo_root``)."""

    path: str
    content: str
    stack: str


def detect_stack(repo_root: Path) -> str | None:
    """Detect the repo's stack from its manifest, or ``None`` when unknown
    (e.g. an empty greenfield repo with no manifest yet)."""
    for name, stack in _MANIFESTS:
        if (repo_root / name).is_file():
            return stack
    return None


def scaffold_gate(repo_root: Path) -> ScaffoldedGate | None:
    """Return the minimal gate to scaffold for ``repo_root``, or ``None``.

    ``None`` when the repo already declares a gate (``discover_gate`` non-empty —
    never clobber the project's own definition of done), a ``ci.yml`` already
    exists at the scaffold path, or the stack is unknown / has no template.
    Pure + offline: reads ``repo_root``, returns the file to write; writes
    nothing itself."""
    if (repo_root / SCAFFOLD_REL_PATH).exists():
        return None
    if not discover_gate(repo_root).is_empty:
        return None
    stack = detect_stack(repo_root)
    if stack is None:
        return None
    # Python's gate is generated from the repo's own dependency declarations
    # (#689); the rest use a static per-stack template.
    content = _python_ci(repo_root) if stack == "python" else _TEMPLATES.get(stack)
    if content is None:
        return None
    return ScaffoldedGate(path=SCAFFOLD_REL_PATH, content=content, stack=stack)


__all__ = [
    "SCAFFOLD_REL_PATH",
    "ScaffoldedGate",
    "detect_stack",
    "scaffold_gate",
]
