"""Tests for the GATED per-product test-PG sidecar in DockerSandboxManager.

Mocks the single ``_docker`` boundary. No real Docker/PG. Two axes:

* OFF (default) — the sandbox ``docker run`` argv is BYTE-IDENTICAL to the
  no-sidecar behavior: no ``--network``, no ``-e``, and no network/sidecar/
  readiness docker calls at all.
* ON — a user-defined network is created idempotently, a blank PG sidecar is
  started on it, ``pg_isready`` gates the sandbox start, the sandbox joins the
  network with the substituted ``-e`` env, and teardown reaps sidecar+network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    SandboxError,
)
from backend.workflow.infrastructure.sandbox import docker_manager as dm


@dataclass
class ScriptedDocker:
    """Records every ``_docker`` call; scriptable per-name inspect + pg_isready.

    ``running`` maps a container name → ``.State.Running`` bool (default: absent
    ⇒ not running). ``pg_ready`` sets the ``pg_isready`` exit code (0 = ready)."""

    running: dict[str, bool] = field(default_factory=dict)
    pg_ready_code: int = 0
    network_create_code: int = 0
    network_create_err: bytes = b""
    run_code: int = 0
    sidecar_run_code: int = 0
    calls: list[list[str]] = field(default_factory=list)

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> tuple[int | None, bytes, bytes]:
        self.calls.append(argv)
        sub = argv[0]
        if sub == "version":
            return (0, b"24.0.0\n", b"")
        if sub == "inspect":
            name = argv[-1]
            return (0, b"true\n" if self.running.get(name, False) else b"false\n", b"")
        if sub == "rm":
            return (0, b"", b"")
        if sub == "network":
            if argv[1] == "create":
                return (self.network_create_code, b"", self.network_create_err)
            return (0, b"", b"")
        if sub == "exec":
            # exec <name> pg_isready -U <user>
            if "pg_isready" in argv:
                return (self.pg_ready_code, b"", b"")
            return (0, b"out", b"")
        if sub == "run":
            # sidecar run has the pgvector image / POSTGRES_* env; sandbox run has -v /work
            is_sidecar = any(a.startswith("POSTGRES_USER=") for a in argv)
            code = self.sidecar_run_code if is_sidecar else self.run_code
            return (code, b"", b"" if code == 0 else b"boom")
        return (0, b"", b"")


def _mgr(
    *,
    test_db_enabled: bool,
    running: dict[str, bool] | None = None,
    pg_ready_code: int = 0,
    ready_timeout_s: float = 60.0,
    network_create_code: int = 0,
    network_create_err: bytes = b"",
    sidecar_run_code: int = 0,
) -> tuple[DockerSandboxManager, ScriptedDocker]:
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="bsvibe-sandbox:test",
        idle_reap_seconds=10,
        max_concurrent=2,
        test_db_enabled=test_db_enabled,
        test_db_image="pgvector/pgvector:pg16",
        test_db_superuser="bsvibe",
        test_db_password="bsvibe",
        test_db_name="bsvibe",
        test_db_env={
            "BSVIBE_DATABASE_URL": "postgresql+asyncpg://bsvibe_app:bsvibe_app_ci@{host}:5432/bsvibe",
            "BSVIBE_MIGRATION_DATABASE_URL": "postgresql+asyncpg://bsvibe:bsvibe@{host}:5432/bsvibe",
            "BSVIBE_APP_DB_PASSWORD": "bsvibe_app_ci",
        },
        test_db_ready_timeout_s=ready_timeout_s,
    )
    fake = ScriptedDocker(
        running=running or {},
        pg_ready_code=pg_ready_code,
        network_create_code=network_create_code,
        network_create_err=network_create_err,
        sidecar_run_code=sidecar_run_code,
    )
    mgr._docker = fake  # type: ignore[method-assign]
    return mgr, fake


def _sandbox_run_argv(fake: ScriptedDocker, sandbox_name: str) -> list[str]:
    return next(
        argv
        for argv in fake.calls
        if argv[0] == "run" and "--name" in argv and sandbox_name in argv
    )


class TestOffPathByteIdentical:
    """The gate OFF (default) must be byte-identical to no-sidecar behavior."""

    async def test_off_path_run_argv_is_exactly_current_behavior(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=False)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        name = f"bsvibe-sbx-{pid}"
        run_argv = _sandbox_run_argv(fake, name)
        assert run_argv == [
            "run",
            "-d",
            "--name",
            name,
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "-v",
            f"{tmp_path}:/work",
            "-w",
            "/work",
            "bsvibe-sandbox:test",
            "sleep",
            "infinity",
        ]

    async def test_off_path_issues_no_network_or_sidecar_calls(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=False)
        await mgr.acquire(uuid.uuid4(), str(tmp_path))
        assert not any(argv[0] == "network" for argv in fake.calls)
        assert not any("pg_isready" in argv for argv in fake.calls)
        # Only one `run` (the sandbox); no sidecar run.
        assert sum(1 for argv in fake.calls if argv[0] == "run") == 1

    async def test_off_path_records_no_sidecar_on_entry(self, tmp_path):
        mgr, _ = _mgr(test_db_enabled=False)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        entry = mgr._containers[pid]  # noqa: SLF001
        assert entry.sidecar is None
        assert entry.network is None


class TestOnPathCreate:
    async def test_creates_network_idempotently_before_sidecar(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        net_calls = [argv for argv in fake.calls if argv[:2] == ["network", "create"]]
        assert net_calls == [["network", "create", f"sbxnet-{pid}"]]

    async def test_network_already_exists_is_tolerated(self, tmp_path):
        mgr, _ = _mgr(
            test_db_enabled=True,
            network_create_code=1,
            network_create_err=b"Error response: network sbxnet-x already exists",
        )
        # Should not raise despite non-zero network create rc.
        await mgr.acquire(uuid.uuid4(), str(tmp_path))

    async def test_network_create_hard_error_raises(self, tmp_path):
        mgr, _ = _mgr(
            test_db_enabled=True,
            network_create_code=1,
            network_create_err=b"daemon unreachable",
        )
        with pytest.raises(SandboxError, match="network create failed"):
            await mgr.acquire(uuid.uuid4(), str(tmp_path))

    async def test_starts_sidecar_with_postgres_env_and_network(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        sidecar_name = f"bsvibe-sbx-pg-{pid}"
        sidecar_run = next(argv for argv in fake.calls if argv[0] == "run" and sidecar_name in argv)
        assert "--network" in sidecar_run
        assert sidecar_run[sidecar_run.index("--network") + 1] == f"sbxnet-{pid}"
        assert "POSTGRES_USER=bsvibe" in sidecar_run
        assert "POSTGRES_PASSWORD=bsvibe" in sidecar_run
        assert "POSTGRES_DB=bsvibe" in sidecar_run
        assert "pgvector/pgvector:pg16" in sidecar_run
        assert "--memory" in sidecar_run

    async def test_polls_pg_isready_before_starting_sandbox(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True, pg_ready_code=0)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        # pg_isready must appear, and the sandbox run must come AFTER it.
        ready_idx = next(i for i, argv in enumerate(fake.calls) if "pg_isready" in argv)
        sandbox_idx = next(
            i
            for i, argv in enumerate(fake.calls)
            if argv[0] == "run" and f"bsvibe-sbx-{pid}" in argv
        )
        assert ready_idx < sandbox_idx

    async def test_sandbox_joins_network_with_substituted_env(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        sidecar_name = f"bsvibe-sbx-pg-{pid}"
        run_argv = _sandbox_run_argv(fake, f"bsvibe-sbx-{pid}")
        assert "--network" in run_argv
        assert run_argv[run_argv.index("--network") + 1] == f"sbxnet-{pid}"
        # {host} substituted with the sidecar container name (container DNS).
        assert (
            f"BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe_app:bsvibe_app_ci@{sidecar_name}:5432/bsvibe"
            in run_argv
        )
        assert (
            f"BSVIBE_MIGRATION_DATABASE_URL=postgresql+asyncpg://bsvibe:bsvibe@{sidecar_name}:5432/bsvibe"
            in run_argv
        )
        assert "BSVIBE_APP_DB_PASSWORD=bsvibe_app_ci" in run_argv
        # No literal {host} left unsubstituted.
        assert not any("{host}" in a for a in run_argv)

    async def test_records_sidecar_and_network_on_entry(self, tmp_path):
        mgr, _ = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        entry = mgr._containers[pid]  # noqa: SLF001
        assert entry.sidecar == f"bsvibe-sbx-pg-{pid}"
        assert entry.network == f"sbxnet-{pid}"


class TestReadinessTimeout:
    async def test_pg_never_ready_raises_sandbox_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dm, "_PG_READY_POLL_S", 0.001)
        mgr, _ = _mgr(test_db_enabled=True, pg_ready_code=1, ready_timeout_s=0.02)
        with pytest.raises(SandboxError, match="not ready"):
            await mgr.acquire(uuid.uuid4(), str(tmp_path))

    async def test_readiness_timeout_releases_permit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dm, "_PG_READY_POLL_S", 0.001)
        mgr, _ = _mgr(test_db_enabled=True, pg_ready_code=1, ready_timeout_s=0.02)
        with pytest.raises(SandboxError):
            await mgr.acquire(uuid.uuid4(), str(tmp_path))
        assert mgr._semaphore._value == 2  # noqa: SLF001


class TestSidecarReuse:
    async def test_running_sidecar_is_not_recreated(self, tmp_path):
        pid = uuid.uuid4()
        sidecar_name = f"bsvibe-sbx-pg-{pid}"
        mgr, fake = _mgr(test_db_enabled=True, running={sidecar_name: True})
        await mgr.acquire(pid, str(tmp_path))
        sidecar_runs = [argv for argv in fake.calls if argv[0] == "run" and sidecar_name in argv]
        assert sidecar_runs == []  # reused, not recreated
        # Network create is still attempted (idempotent) — that is fine.


class TestTeardownReapsSidecar:
    async def test_release_removes_sidecar_and_network(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        fake.calls.clear()
        await mgr.release(pid)
        assert ["rm", "-f", f"bsvibe-sbx-pg-{pid}"] in fake.calls
        assert ["network", "rm", f"sbxnet-{pid}"] in fake.calls

    async def test_reap_idle_removes_sidecar_and_network(self, tmp_path):
        mgr, fake = _mgr(test_db_enabled=True)
        pid = uuid.uuid4()
        await mgr.acquire(pid, str(tmp_path))
        for entry in mgr._containers.values():  # noqa: SLF001
            entry.last_used = 0.0
        fake.calls.clear()
        await mgr.reap_idle()
        assert ["rm", "-f", f"bsvibe-sbx-pg-{pid}"] in fake.calls
        assert ["network", "rm", f"sbxnet-{pid}"] in fake.calls
        assert mgr._containers == {}  # noqa: SLF001
