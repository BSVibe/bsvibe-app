"""L-I1a — discover a repo's OWN acceptance gate (stack-agnostic).

BSVibe must not hardcode a stack-specific check list (ruff/mypy/pytest). It
defers "what is good" to the target: the commands the repo already declares to
verify itself. This module parses those out of the repo's own config, in
priority order (the repo's real CI is the most authoritative source), and
returns an empty gate when the repo declares none (→ honesty grade "no gate").
"""

from __future__ import annotations

from pathlib import Path

from backend.workflow.domain.gate_discovery import discover_gate


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


class TestGithubActions:
    def test_parses_run_steps_from_ci_workflow(self, tmp_path: Path):
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            """
name: CI
on: [push]
jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Ruff (lint)
        run: uv run ruff check backend/ tests/
      - name: mypy
        run: uv run mypy backend/
      - name: pytest
        run: uv run pytest -q
""",
        )
        gate = discover_gate(tmp_path)
        assert gate.origin == "github-actions"
        cmds = [c.command for c in gate.commands]
        assert "uv run ruff check backend/ tests/" in cmds
        assert "uv run mypy backend/" in cmds
        assert "uv run pytest -q" in cmds
        # provenance points back at the workflow file
        assert all(".github/workflows/ci.yml" in c.source for c in gate.commands)

    def test_multiline_run_block_is_split_into_commands(self, tmp_path: Path):
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            """
jobs:
  build:
    steps:
      - run: |
          npm ci
          npm run build
""",
        )
        gate = discover_gate(tmp_path)
        cmds = [c.command for c in gate.commands]
        assert "npm ci" in cmds
        assert "npm run build" in cmds


class TestFallbacksInPriorityOrder:
    def test_makefile_when_no_ci(self, tmp_path: Path):
        _write(tmp_path, "Makefile", "test:\n\tpytest\n\nlint:\n\truff check .\n")
        gate = discover_gate(tmp_path)
        assert gate.origin == "makefile"
        cmds = {c.command for c in gate.commands}
        assert "make test" in cmds
        assert "make lint" in cmds

    def test_package_json_scripts(self, tmp_path: Path):
        _write(
            tmp_path,
            "package.json",
            '{"scripts": {"test": "vitest run", "lint": "biome check .", "build": "tsc"}}',
        )
        gate = discover_gate(tmp_path)
        assert gate.origin == "package.json"
        cmds = {c.command for c in gate.commands}
        assert "npm run test" in cmds
        assert "npm run lint" in cmds
        assert "npm run build" in cmds

    def test_cargo_manifest_gives_canonical_commands(self, tmp_path: Path):
        _write(tmp_path, "Cargo.toml", '[package]\nname = "x"\n')
        gate = discover_gate(tmp_path)
        assert gate.origin == "cargo"
        cmds = {c.command for c in gate.commands}
        assert "cargo test" in cmds
        assert "cargo build" in cmds

    def test_go_module_gives_canonical_commands(self, tmp_path: Path):
        _write(tmp_path, "go.mod", "module example.com/x\n\ngo 1.22\n")
        gate = discover_gate(tmp_path)
        assert gate.origin == "go"
        cmds = {c.command for c in gate.commands}
        assert "go test ./..." in cmds
        assert "go build ./..." in cmds

    def test_ci_wins_over_makefile(self, tmp_path: Path):
        _write(tmp_path, "Makefile", "test:\n\tpytest\n")
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            "jobs:\n  t:\n    steps:\n      - run: uv run pytest\n",
        )
        gate = discover_gate(tmp_path)
        assert gate.origin == "github-actions"


class TestNoGate:
    def test_empty_repo_has_no_gate(self, tmp_path: Path):
        gate = discover_gate(tmp_path)
        assert gate.origin == "none"
        assert gate.is_empty
        assert gate.commands == ()
