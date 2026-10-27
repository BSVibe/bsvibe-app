"""``changed_paths`` against a REAL git repo: a new directory is not folded into one entry."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.workflow.application.inplace_gate import changed_paths
from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio


class _LocalBox:
    """Runs the shell command in a real directory — git answers for itself."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=out.decode(),
            stderr=err.decode(),
            timed_out=False,
        )


async def test_new_directory_files_listed_individually(tmp_path: Path) -> None:
    box = _LocalBox(tmp_path)
    assert (await box.exec("git init -q", timeout_s=30)).exit_code == 0
    (tmp_path / "newpkg" / "sub").mkdir(parents=True)
    (tmp_path / "newpkg" / "mod.py").write_text("a = 1\n")
    (tmp_path / "newpkg" / "sub" / "b.py").write_text("b = 2\n")

    paths = await changed_paths(box, None)  # type: ignore[arg-type]

    assert paths == ["newpkg/mod.py", "newpkg/sub/b.py"]
