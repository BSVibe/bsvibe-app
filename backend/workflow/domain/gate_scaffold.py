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

from dataclasses import dataclass
from pathlib import Path

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

_PYTHON_CI = """\
name: CI
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: pip install -e . ruff
      - name: Lint
        run: ruff check .
      - name: Format
        run: ruff format --check .
      - name: Test
        run: pytest
"""

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

_TEMPLATES: dict[str, str] = {
    "python": _PYTHON_CI,
    "node": _NODE_CI,
    "go": _GO_CI,
    "rust": _RUST_CI,
}


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
    content = _TEMPLATES.get(stack)
    if content is None:
        return None
    return ScaffoldedGate(path=SCAFFOLD_REL_PATH, content=content, stack=stack)


__all__ = [
    "SCAFFOLD_REL_PATH",
    "ScaffoldedGate",
    "detect_stack",
    "scaffold_gate",
]
