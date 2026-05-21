"""Tests for NoopSandboxManager — host-side fallback."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.supervisor.sandbox import (
    NoopSandboxManager,
    NoopSandboxSession,
    SandboxError,
)


class TestExec:
    async def test_runs_simple_command(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        result = await s.exec("echo hello", timeout_s=5.0)
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert result.timed_out is False

    async def test_shell_true_runs_through_sh(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        result = await s.exec("echo $((1+1))", timeout_s=5.0, shell=True)
        assert result.exit_code == 0
        assert "2" in result.stdout

    async def test_command_not_found_returns_127(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        result = await s.exec("definitely-not-a-command", timeout_s=5.0)
        assert result.exit_code == 127
        assert "command not found" in result.stderr

    async def test_empty_command_raises(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="empty"):
            await s.exec("   ", timeout_s=5.0)

    async def test_bad_shell_syntax_raises(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="bad shell syntax"):
            await s.exec('echo "unterminated', timeout_s=5.0)

    async def test_timeout_sets_timed_out(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        result = await s.exec("sleep 5", timeout_s=0.1)
        assert result.timed_out is True
        assert result.exit_code is None


class TestFileOps:
    async def test_write_and_read_file(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        await s.write_file("greeting.txt", b"hi")
        data = await s.read_file("greeting.txt", max_bytes=1024)
        assert data == b"hi"

    async def test_write_creates_parents(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        await s.write_file("nested/dir/file.txt", b"x")
        assert (tmp_path / "nested" / "dir" / "file.txt").read_bytes() == b"x"

    async def test_read_respects_cap(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        await s.write_file("big.txt", b"a" * 100)
        data = await s.read_file("big.txt", max_bytes=10)
        assert len(data) == 10

    async def test_path_escape_rejected_on_read(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="escapes"):
            await s.read_file("../escape.txt", max_bytes=10)

    async def test_path_escape_rejected_on_write(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="escapes"):
            await s.write_file("../bad.txt", b"x")

    async def test_path_escape_rejected_on_list(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="escapes"):
            await s.list_dir("..")

    async def test_list_dir_returns_entries(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "subdir").mkdir()
        entries = await s.list_dir(".")
        assert "a.txt" in entries
        assert "subdir/" in entries

    async def test_list_dir_not_a_directory(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        (tmp_path / "f").write_text("x")
        with pytest.raises(SandboxError, match="not a directory"):
            await s.list_dir("f")

    async def test_read_missing_file_raises(self, tmp_path: Path):
        s = NoopSandboxSession(str(tmp_path))
        with pytest.raises(SandboxError, match="read_file"):
            await s.read_file("nope.txt", max_bytes=10)


class TestManagerLifecycle:
    async def test_acquire_returns_session(self, tmp_path: Path):
        mgr = NoopSandboxManager()
        s = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        assert s.workspace_mount == str(tmp_path.resolve())  # noqa: ASYNC240

    async def test_release_is_noop(self, tmp_path: Path):
        mgr = NoopSandboxManager()
        await mgr.release(uuid.uuid4())

    async def test_reap_idle_is_noop(self, tmp_path: Path):
        await NoopSandboxManager().reap_idle()

    async def test_health_true(self, tmp_path: Path):
        assert await NoopSandboxManager().health() is True
