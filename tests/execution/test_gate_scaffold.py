"""Unit tests for I1 gate scaffolding (pure, offline).

The critical property is the ROUND TRIP: a scaffolded gate must be parseable by
:func:`discover_gate` (so I1 actually has commands to run afterwards), and
scaffolding must NEVER clobber a repo that already declares its own gate.
"""

from __future__ import annotations

from pathlib import Path

from backend.workflow.domain.gate_discovery import discover_gate
from backend.workflow.domain.gate_scaffold import (
    SCAFFOLD_REL_PATH,
    detect_stack,
    scaffold_gate,
)


def _write(root: Path, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ── detect_stack ─────────────────────────────────────────────────────────────


def test_detect_python(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert detect_stack(tmp_path) == "python"


def test_detect_node(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", "{}")
    assert detect_stack(tmp_path) == "node"


def test_detect_none_for_empty_repo(tmp_path: Path) -> None:
    assert detect_stack(tmp_path) is None


def test_detect_priority_python_over_node(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "package.json", "{}")
    assert detect_stack(tmp_path) == "python"


# ── scaffold_gate: the gap it fills (python without CI) ──────────────────────


def test_scaffolds_python_when_no_gate(tmp_path: Path) -> None:
    # A python project with only a pyproject.toml — discover_gate has no python
    # detector, so it declares NO gate. This is the exact gap I1c fills.
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert discover_gate(tmp_path).is_empty  # premise
    gate = scaffold_gate(tmp_path)
    assert gate is not None
    assert gate.stack == "python"
    assert gate.path == SCAFFOLD_REL_PATH


def test_scaffolded_python_gate_round_trips_through_discover(tmp_path: Path) -> None:
    # THE key invariant: after writing the scaffold, discover_gate must parse it
    # into runnable commands — otherwise I1 still has nothing to run.
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    gate = scaffold_gate(tmp_path)
    assert gate is not None
    _write(tmp_path, gate.path, gate.content)

    discovered = discover_gate(tmp_path)
    assert discovered.origin == "github-actions"
    cmds = [c.command for c in discovered.commands]
    assert "ruff check ." in cmds
    assert "ruff format --check ." in cmds


def test_scaffolds_node_when_package_json_has_no_gate_scripts(tmp_path: Path) -> None:
    # package.json with no lint/test/build scripts → discover_gate empty.
    _write(tmp_path, "package.json", '{"name":"x","scripts":{"start":"node ."}}')
    assert discover_gate(tmp_path).is_empty  # premise
    gate = scaffold_gate(tmp_path)
    assert gate is not None and gate.stack == "node"


# ── scaffold_gate: never clobbers an existing gate ───────────────────────────


def test_no_scaffold_when_repo_already_has_ci(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs:\n  j:\n    steps:\n      - run: ruff check .\n",
    )
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert scaffold_gate(tmp_path) is None


def test_no_scaffold_when_package_json_declares_a_gate(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"scripts":{"lint":"eslint .","test":"vitest"}}')
    assert not discover_gate(tmp_path).is_empty  # premise: it has its own gate
    assert scaffold_gate(tmp_path) is None


def test_no_scaffold_for_go_repo_it_already_has_a_gate(tmp_path: Path) -> None:
    # go.mod → discover_gate emits canonical go commands, so it is NON-empty and
    # scaffolding is a no-op (the repo keeps its own gate).
    _write(tmp_path, "go.mod", "module x\n")
    assert not discover_gate(tmp_path).is_empty
    assert scaffold_gate(tmp_path) is None


def test_no_scaffold_for_empty_repo(tmp_path: Path) -> None:
    # No manifest → no stack to detect → nothing to scaffold (honest: the stack
    # only materializes as the project grows).
    assert scaffold_gate(tmp_path) is None


def test_no_clobber_when_scaffold_file_exists_but_has_no_run_steps(tmp_path: Path) -> None:
    # An existing ci.yml with no ``run:`` steps leaves discover_gate empty, but
    # we must not overwrite the user's file.
    _write(tmp_path, ".github/workflows/ci.yml", "name: deploy\non: push\n")
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert scaffold_gate(tmp_path) is None
