"""Tests for DockerSandboxManager via mocked ``_docker`` boundary.

The FakeDocker pattern matches BSNexus's tests verbatim — every docker
CLI invocation is captured + scriptable. No real subprocess calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    SandboxError,
    SandboxUnavailable,
)


@dataclass
class FakeDocker:
    """Records every ``_docker`` invocation; scriptable per subcommand."""

    version_ok: bool = True
    running: bool = True
    run_code: int = 0
    exec_result: tuple[int | None, bytes, bytes] = (0, b"out", b"")
    timed_out_first: bool = False
    calls: list[tuple[list[str], bytes | None]] = field(default_factory=list)

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> tuple[int | None, bytes, bytes]:
        self.calls.append((argv, stdin))
        sub = argv[0]
        if sub == "version":
            return (0 if self.version_ok else 1, b"24.0.0\n", b"")
        if sub == "inspect":
            return (0, b"true\n" if self.running else b"false\n", b"")
        if sub == "rm":
            return (0, b"", b"")
        if sub == "run":
            return (self.run_code, b"", b"created\n" if self.run_code == 0 else b"boom")
        if sub == "exec":
            return self.exec_result
        return (0, b"", b"")


def _mgr_with_fake(*, sandbox_user: str = "", **kwargs) -> tuple[DockerSandboxManager, FakeDocker]:
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="bsvibe-sandbox:test",
        idle_reap_seconds=10,
        max_concurrent=2,
        sandbox_user=sandbox_user,
    )
    fake = FakeDocker(**kwargs)
    mgr._docker = fake  # type: ignore[method-assign]
    return mgr, fake


class TestHealth:
    async def test_version_ok_returns_true(self):
        mgr, _ = _mgr_with_fake()
        assert await mgr.health() is True

    async def test_version_nonzero_returns_false(self):
        mgr, _ = _mgr_with_fake(version_ok=False)
        assert await mgr.health() is False


class TestAcquireCreate:
    async def test_creates_container_with_expected_argv(self, tmp_path):
        mgr, fake = _mgr_with_fake()
        pid = uuid.uuid4()
        session = await mgr.acquire(pid, str(tmp_path))
        assert session.workspace_mount == "/work"

        run_calls = [argv for argv, _ in fake.calls if argv[0] == "run"]
        assert len(run_calls) == 1
        argv = run_calls[0]
        assert "--name" in argv
        assert f"bsvibe-sbx-{pid}" in argv
        assert "-v" in argv
        assert f"{tmp_path}:/work" in argv
        assert "sleep" in argv and "infinity" in argv
        assert "--memory" in argv

    async def test_reuses_existing_running_container(self, tmp_path):
        mgr, fake = _mgr_with_fake()
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        first_call_count = len(fake.calls)
        await mgr.acquire(pid, str(tmp_path))
        new_calls = fake.calls[first_call_count:]
        assert not any(argv[0] == "run" for argv, _ in new_calls)

    async def test_dind_unreachable_raises_unavailable(self, tmp_path):
        mgr, _ = _mgr_with_fake(version_ok=False)
        # shorten startup timeout via monkey-patch to keep the test fast
        from backend.workflow.infrastructure.sandbox import docker_manager as dm

        dm._DIND_STARTUP_TIMEOUT_S = 0.1  # noqa: SLF001
        try:
            with pytest.raises(SandboxUnavailable):
                await mgr.acquire(uuid.uuid4(), str(tmp_path))
        finally:
            dm._DIND_STARTUP_TIMEOUT_S = 30.0  # noqa: SLF001

    async def test_create_failure_releases_permit(self, tmp_path):
        mgr, _ = _mgr_with_fake(run_code=1)
        with pytest.raises(SandboxError, match="sandbox create failed"):
            await mgr.acquire(uuid.uuid4(), str(tmp_path))
        # Permit should be released even though create failed.
        assert mgr._semaphore._value == 2  # noqa: SLF001


class TestSandboxUser:
    """``sandbox_user`` adds an EXPLICIT ``--user`` run flag so the per-project
    sandbox runs as a uid that can write the root-owned run workspace mounted at
    ``/work`` (the worker writes the worktree as root; the image default user is
    uid 1000 → unwritable). Empty setting = image default, no ``--user`` (no
    silent uid coercion — explicit config only)."""

    async def test_user_set_adds_user_flag(self, tmp_path):
        mgr, fake = _mgr_with_fake(sandbox_user="0:0")
        await mgr.acquire(uuid.uuid4(), str(tmp_path))
        run_argv = next(argv for argv, _ in fake.calls if argv[0] == "run")
        assert "--user" in run_argv
        assert run_argv[run_argv.index("--user") + 1] == "0:0"

    async def test_user_empty_omits_user_flag(self, tmp_path):
        mgr, fake = _mgr_with_fake(sandbox_user="")
        await mgr.acquire(uuid.uuid4(), str(tmp_path))
        run_argv = next(argv for argv, _ in fake.calls if argv[0] == "run")
        assert "--user" not in run_argv


class TestExecPath:
    async def test_exec_runs_docker_exec(self, tmp_path):
        mgr, fake = _mgr_with_fake(exec_result=(0, b"line1\nline2\n", b""))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        result = await session.exec("ls -l", timeout_s=10.0)
        assert result.exit_code == 0
        assert "line1" in result.stdout
        last_argv = fake.calls[-1][0]
        assert last_argv[0] == "exec"
        assert "-w" in last_argv
        assert "/work" in last_argv
        assert last_argv[-2:] == ["ls", "-l"]

    async def test_shell_true_wraps_in_sh_c(self, tmp_path):
        mgr, fake = _mgr_with_fake(exec_result=(0, b"", b""))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        await session.exec("echo $((1+1))", timeout_s=10.0, shell=True)
        last_argv = fake.calls[-1][0]
        assert last_argv[-3] == "sh"
        assert last_argv[-2] == "-c"

    async def test_empty_command_raises(self, tmp_path):
        mgr, _ = _mgr_with_fake()
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="empty"):
            await session.exec("   ", timeout_s=10.0)

    async def test_bad_shell_syntax_raises(self, tmp_path):
        mgr, _ = _mgr_with_fake()
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="bad shell syntax"):
            await session.exec('echo "unterm', timeout_s=10.0)

    async def test_exec_timeout_returns_timed_out(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(None, b"", b"docker timed out after 5.0s"))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        result = await session.exec("sleep 5", timeout_s=5.0)
        assert result.timed_out is True
        assert result.exit_code is None


class TestFileOps:
    async def test_write_file_via_stdin(self, tmp_path):
        mgr, fake = _mgr_with_fake(exec_result=(0, b"", b""))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        await session.write_file("dir/file.txt", b"hello")
        # The last call should be an exec carrying the file content as stdin.
        last_argv, last_stdin = fake.calls[-1]
        assert last_argv[0] == "exec"
        assert "-i" in last_argv
        assert last_stdin == b"hello"

    async def test_read_file_returns_capped_output(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(0, b"a" * 100, b""))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        data = await session.read_file("any.txt", max_bytes=10)
        assert len(data) == 10

    async def test_read_failure_raises(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(1, b"", b"no such file"))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="read_file"):
            await session.read_file("missing", max_bytes=10)

    async def test_write_failure_raises(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(1, b"", b"perm denied"))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="write_file"):
            await session.write_file("x", b"x")

    async def test_list_dir_returns_sorted_entries(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(0, b"b\nc/\na\n", b""))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        entries = await session.list_dir(".")
        assert entries == ["a", "b", "c/"]

    async def test_list_dir_failure_raises(self, tmp_path):
        mgr, _ = _mgr_with_fake(exec_result=(1, b"", b"not a dir"))
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="list_dir"):
            await session.list_dir("x")

    async def test_path_escape_rejected(self, tmp_path):
        mgr, _ = _mgr_with_fake()
        session = await mgr.acquire(uuid.uuid4(), str(tmp_path))
        with pytest.raises(SandboxError, match="escapes"):
            await session.read_file("../etc", max_bytes=10)
        with pytest.raises(SandboxError, match="escapes"):
            await session.read_file("/etc/passwd", max_bytes=10)


class TestRelease:
    async def test_release_runs_rm_and_releases_permit(self, tmp_path):
        mgr, fake = _mgr_with_fake()
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        await mgr.release(pid)
        rm_calls = [argv for argv, _ in fake.calls if argv[0] == "rm"]
        # rm is called twice: pre-create cleanup + post-release teardown.
        assert len(rm_calls) >= 2
        assert pid not in mgr._held  # noqa: SLF001
        assert mgr._semaphore._value == 2  # noqa: SLF001

    async def test_release_unknown_project_noop(self):
        mgr, _ = _mgr_with_fake()
        await mgr.release(uuid.uuid4())


class TestReapIdle:
    async def test_reaps_old_containers(self, tmp_path):
        mgr, _ = _mgr_with_fake()
        pid1, pid2 = uuid.uuid4(), uuid.uuid4()
        await mgr.acquire(pid1, str(tmp_path))
        await mgr.acquire(pid2, str(tmp_path))
        # Force both entries to look ancient by zeroing last_used.
        for entry in mgr._containers.values():  # noqa: SLF001
            entry.last_used = 0.0
        await mgr.reap_idle()
        assert mgr._containers == {}  # noqa: SLF001
