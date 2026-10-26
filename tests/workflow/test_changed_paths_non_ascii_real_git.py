"""``changed_paths`` names non-ASCII files as they are on disk, against a real git repo."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from backend.workflow.application.inplace_gate import changed_paths
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult


class _RepoSession:
    """A sandbox session that runs the shell string in a real directory."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> Any:
        proc = subprocess.run(  # noqa: S602, ASYNC221 — the command under test IS a shell string
            command,
            shell=True,
            cwd=self._cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return SandboxResult(proc.returncode, proc.stdout, proc.stderr, False)


def _git(root: Path, command: str) -> None:
    subprocess.run(command, shell=True, cwd=root, check=True, capture_output=True)  # noqa: S602


def test_non_ascii_and_spaced_paths_come_back_unescaped(tmp_path: Path) -> None:
    _git(tmp_path, "git init -q -b main")
    _git(tmp_path, "git config user.email t@t && git config user.name t")
    (tmp_path / "설명.md").write_text("a\n", encoding="utf-8")
    _git(tmp_path, "git add -A && git commit -qm init")

    (tmp_path / "설명.md").write_text("b\n", encoding="utf-8")
    (tmp_path / "새 파일.txt").write_text("c\n", encoding="utf-8")

    paths = asyncio.run(changed_paths(_RepoSession(tmp_path), None))  # type: ignore[arg-type]

    assert paths == sorted(["설명.md", "새 파일.txt"])
