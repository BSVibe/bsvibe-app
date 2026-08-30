"""DinD-backed sandbox manager — the single ``docker`` shell-out site.

Lifted from ``BSNexus/backend/src/core/sandbox/docker_manager.py``;
container prefix renamed ``bsnexus-sbx-`` → ``bsvibe-sbx-``, import
paths rewritten.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import shlex
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import structlog

from backend.workflow.infrastructure.sandbox.errors import SandboxError, SandboxUnavailable
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult

logger = structlog.get_logger(__name__)

_CONTAINER_PREFIX = "bsvibe-sbx-"
_SIDECAR_PREFIX = "bsvibe-sbx-pg-"
_NETWORK_PREFIX = "sbxnet-"
_WORK_MOUNT = "/work"
_DIND_STARTUP_TIMEOUT_S = 30.0
_DOCKER_OP_TIMEOUT_S = 60.0
_SANDBOX_MEMORY = "4g"
_SIDECAR_MEMORY = "1g"
# Poll interval while waiting for the test-PG sidecar's ``pg_isready`` — module
# constant so unit tests can shrink it (no real sleeping against a mock).
_PG_READY_POLL_S = 1.0


def _container_name(project_id: uuid.UUID) -> str:
    return f"{_CONTAINER_PREFIX}{project_id}"


def _sidecar_name(project_id: uuid.UUID) -> str:
    return f"{_SIDECAR_PREFIX}{project_id}"


def _network_name(project_id: uuid.UUID) -> str:
    return f"{_NETWORK_PREFIX}{project_id}"


def _safe_rel(rel_path: str) -> str:
    norm = posixpath.normpath(rel_path or ".")
    if norm.startswith("..") or norm.startswith("/"):
        raise SandboxError(f"path {rel_path!r} escapes the workspace")
    return norm


@dataclass
class _Entry:
    name: str
    last_used: float
    # Test-PG sidecar container + its dedicated user-defined network, tracked so
    # teardown reaps them with the sandbox. ``None`` when the test-db gate is off.
    sidecar: str | None = None
    network: str | None = None


class DockerSandboxSession:
    """A handle to one project's running sandbox container."""

    def __init__(self, *, container: str, docker: DockerSandboxManager) -> None:
        self._container = container
        self._mgr = docker

    @property
    def workspace_mount(self) -> str:
        return _WORK_MOUNT

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> SandboxResult:
        if shell:
            inner = ["sh", "-c", command]
        else:
            try:
                parts = shlex.split(command)
            except ValueError as exc:
                raise SandboxError(f"bad shell syntax: {exc}") from exc
            if not parts:
                raise SandboxError("empty command")
            inner = parts
        code, out, err = await self._mgr._docker(
            ["exec", "-w", _WORK_MOUNT, self._container, *inner],
            timeout_s=timeout_s,
        )
        return SandboxResult(
            exit_code=code,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            timed_out=code is None,
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        norm = _safe_rel(rel_path)
        code, out, err = await self._mgr._docker(
            ["exec", self._container, "cat", f"{_WORK_MOUNT}/{norm}"],
            timeout_s=_DOCKER_OP_TIMEOUT_S,
        )
        if code != 0:
            raise SandboxError(
                f"read_file: {err.decode('utf-8', errors='replace').strip() or 'failed'}"
            )
        return out[:max_bytes]

    async def write_file(self, rel_path: str, content: bytes) -> None:
        norm = _safe_rel(rel_path)
        target = f"{_WORK_MOUNT}/{norm}"
        script = f'mkdir -p "$(dirname {shlex.quote(target)})" && cat > {shlex.quote(target)}'
        code, _out, err = await self._mgr._docker(
            ["exec", "-i", self._container, "sh", "-c", script],
            timeout_s=_DOCKER_OP_TIMEOUT_S,
            stdin=content,
        )
        if code != 0:
            raise SandboxError(
                f"write_file: {err.decode('utf-8', errors='replace').strip() or 'failed'}"
            )

    async def list_dir(self, rel_path: str) -> list[str]:
        norm = _safe_rel(rel_path)
        code, out, err = await self._mgr._docker(
            [
                "exec",
                self._container,
                "sh",
                "-c",
                f"ls -A -p {shlex.quote(f'{_WORK_MOUNT}/{norm}')}",
            ],
            timeout_s=_DOCKER_OP_TIMEOUT_S,
        )
        if code != 0:
            raise SandboxError(
                f"list_dir: {err.decode('utf-8', errors='replace').strip() or 'failed'}"
            )
        return sorted(line for line in out.decode("utf-8", errors="replace").splitlines() if line)


class DockerSandboxManager:
    """Per-project sandbox lifecycle over a DinD daemon."""

    def __init__(
        self,
        *,
        docker_host: str,
        sandbox_image: str,
        idle_reap_seconds: int,
        max_concurrent: int,
        sandbox_user: str = "",
        test_db_enabled: bool = False,
        test_db_image: str = "pgvector/pgvector:pg16",
        test_db_superuser: str = "bsvibe",
        test_db_password: str = "bsvibe",  # noqa: S107 — blank-PG default, not a secret
        test_db_name: str = "bsvibe",
        test_db_env: dict[str, str] | None = None,
        test_db_ready_timeout_s: float = 60.0,
    ) -> None:
        self._docker_host = docker_host
        self._image = sandbox_image
        # Explicit ``--user`` for the sandbox container. The worker writes the
        # run worktree as root, so the image's default uid-1000 ``sandbox`` user
        # cannot write ``/work``. Setting this to e.g. ``"0:0"`` matches the
        # worker's uid; empty leaves the image default (no ``--user``) — never a
        # silent uid coercion.
        self._user = sandbox_user
        # Per-product test-PG sidecar gate (default OFF). Off ⇒ the sandbox
        # ``docker run`` is byte-identical to no-sidecar: no network, no env.
        self._test_db_enabled = test_db_enabled
        self._test_db_image = test_db_image
        self._test_db_superuser = test_db_superuser
        self._test_db_password = test_db_password
        self._test_db_name = test_db_name
        self._test_db_env = dict(test_db_env or {})
        self._test_db_ready_timeout_s = test_db_ready_timeout_s
        self._idle_reap_seconds = idle_reap_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._containers: dict[uuid.UUID, _Entry] = {}
        self._held: set[uuid.UUID] = set()
        # 프로세스당 1회 — 고아 스윕은 기동 시 한 번이면 충분하다.
        self._swept = False
        self._registry_lock = asyncio.Lock()

    async def _docker(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> tuple[int | None, bytes, bytes]:
        """The single docker-CLI boundary; the unit-test mock point."""
        env = dict(os.environ)
        if self._docker_host:
            env["DOCKER_HOST"] = self._docker_host
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return None, b"", f"docker timed out after {timeout_s}s".encode()
        return proc.returncode, out, err

    async def health(self) -> bool:
        code, _out, _err = await self._docker(
            ["version", "--format", "{{.Server.Version}}"], timeout_s=10.0
        )
        return code == 0

    async def _await_dind(self) -> None:
        deadline = time.monotonic() + _DIND_STARTUP_TIMEOUT_S
        while True:
            if await self.health():
                # First contact in this process: reap what a previous process
                # left behind. Wiring it HERE (not in a worker) is deliberate —
                # the sweep needs a reachable daemon, and this is the one place
                # that already waits for one.
                if not self._swept:
                    self._swept = True
                    await self.sweep_orphans()
                return
            if time.monotonic() >= deadline:
                raise SandboxUnavailable(
                    f"sandbox DinD unreachable at {self._docker_host or '(default)'} "
                    f"after {_DIND_STARTUP_TIMEOUT_S}s"
                )
            await asyncio.sleep(1.0)

    async def _is_running(self, name: str) -> bool:
        code, out, _err = await self._docker(
            ["inspect", "-f", "{{.State.Running}}", name], timeout_s=10.0
        )
        return code == 0 and out.decode().strip() == "true"

    async def acquire(self, project_id: uuid.UUID, workspace_path: str) -> DockerSandboxSession:
        async with self._registry_lock:
            lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            entry = self._containers.get(project_id)
            if entry is not None and await self._is_running(entry.name):
                entry.last_used = time.monotonic()
                return DockerSandboxSession(container=entry.name, docker=self)
            if entry is not None:
                await self._teardown(project_id)
            return await self._create(project_id, workspace_path)

    async def _ensure_network(self, network: str) -> None:
        """Idempotently create the sandbox's user-defined bridge network.

        The user-defined network is the DinD firewall escape: the DROP rule
        that blocks private-range traffic is scoped to the DEFAULT bridge
        (``-i docker0``), so a dedicated network reaches the sidecar. Tolerate
        the "already exists" race (a reused sidecar's network survives)."""
        code, _out, err = await self._docker(
            ["network", "create", network], timeout_s=_DOCKER_OP_TIMEOUT_S
        )
        if code == 0:
            return
        text = err.decode("utf-8", errors="replace")
        if "already exists" in text:
            return
        raise SandboxError(f"sandbox network create failed: {text.strip()}")

    async def _ensure_sidecar(self, network: str, sidecar: str) -> None:
        """Start the blank test-PG sidecar on ``network`` (reuse if running).

        The sidecar is a BLANK ``pgvector`` with a single superuser/owner role;
        the product's OWN migration chain creates any runtime role — no product
        role model is hardcoded here."""
        if await self._is_running(sidecar):
            return
        await self._docker(["rm", "-f", sidecar], timeout_s=_DOCKER_OP_TIMEOUT_S)
        code, _out, err = await self._docker(
            [
                "run",
                "-d",
                "--name",
                sidecar,
                "--network",
                network,
                "--memory",
                _SIDECAR_MEMORY,
                "-e",
                f"POSTGRES_USER={self._test_db_superuser}",
                "-e",
                f"POSTGRES_PASSWORD={self._test_db_password}",
                "-e",
                f"POSTGRES_DB={self._test_db_name}",
                self._test_db_image,
            ],
            timeout_s=_DOCKER_OP_TIMEOUT_S,
        )
        if code != 0:
            raise SandboxError(
                f"sandbox test-db create failed: {err.decode('utf-8', errors='replace').strip()}"
            )

    async def _await_pg_ready(self, sidecar: str) -> None:
        """Poll ``pg_isready`` until rc==0 or the ready-timeout — fail honestly.

        A timeout raises rather than handing the agent a half-up DB."""
        deadline = time.monotonic() + self._test_db_ready_timeout_s
        while True:
            code, _out, _err = await self._docker(
                ["exec", sidecar, "pg_isready", "-U", self._test_db_superuser],
                timeout_s=_DOCKER_OP_TIMEOUT_S,
            )
            if code == 0:
                return
            if time.monotonic() >= deadline:
                raise SandboxError(
                    f"sandbox test-db {sidecar} not ready after {self._test_db_ready_timeout_s}s"
                )
            await asyncio.sleep(_PG_READY_POLL_S)

    def _test_db_env_flags(self, host: str) -> list[str]:
        """``-e KEY=VALUE`` flags for the sandbox, ``{host}`` → sidecar DNS name."""
        flags: list[str] = []
        for key, value in self._test_db_env.items():
            flags.extend(["-e", f"{key}={value.replace('{host}', host)}"])
        return flags

    async def _create(self, project_id: uuid.UUID, workspace_path: str) -> DockerSandboxSession:
        await self._await_dind()
        name = _container_name(project_id)
        await self._docker(["rm", "-f", name], timeout_s=_DOCKER_OP_TIMEOUT_S)
        await self._semaphore.acquire()
        sidecar: str | None = None
        network: str | None = None
        net_flag: list[str] = []
        env_flags: list[str] = []
        try:
            if self._test_db_enabled:
                network = _network_name(project_id)
                sidecar = _sidecar_name(project_id)
                await self._ensure_network(network)
                await self._ensure_sidecar(network, sidecar)
                await self._await_pg_ready(sidecar)
                net_flag = ["--network", network]
                env_flags = self._test_db_env_flags(sidecar)
            user_flag = ["--user", self._user] if self._user else []
            code, _out, err = await self._docker(
                [
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--memory",
                    _SANDBOX_MEMORY,
                    "--memory-swap",
                    _SANDBOX_MEMORY,
                    *net_flag,
                    *env_flags,
                    *user_flag,
                    "-v",
                    f"{workspace_path}:{_WORK_MOUNT}",
                    "-w",
                    _WORK_MOUNT,
                    self._image,
                    "sleep",
                    "infinity",
                ],
                timeout_s=_DOCKER_OP_TIMEOUT_S,
            )
        except BaseException:
            self._semaphore.release()
            raise
        if code != 0:
            self._semaphore.release()
            raise SandboxError(
                f"sandbox create failed: {err.decode('utf-8', errors='replace').strip()}"
            )
        self._containers[project_id] = _Entry(
            name=name, last_used=time.monotonic(), sidecar=sidecar, network=network
        )
        self._held.add(project_id)
        logger.info("sandbox_created", project_id=str(project_id), container=name)
        return DockerSandboxSession(container=name, docker=self)

    async def _teardown(self, project_id: uuid.UUID) -> None:
        entry = self._containers.pop(project_id, None)
        if entry is not None:
            await self._docker(["rm", "-f", entry.name], timeout_s=_DOCKER_OP_TIMEOUT_S)
            # Reap the test-PG sidecar + its network with the sandbox (tolerate
            # absent — best-effort ``rm``/``network rm`` ignore rc).
            if entry.sidecar is not None:
                await self._docker(["rm", "-f", entry.sidecar], timeout_s=_DOCKER_OP_TIMEOUT_S)
            if entry.network is not None:
                await self._docker(["network", "rm", entry.network], timeout_s=_DOCKER_OP_TIMEOUT_S)
            logger.info("sandbox_removed", project_id=str(project_id), container=entry.name)
        if project_id in self._held:
            self._held.discard(project_id)
            self._semaphore.release()

    async def release(self, project_id: uuid.UUID) -> None:
        async with self._registry_lock:
            lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            await self._teardown(project_id)

    async def sweep_orphans(self) -> None:
        """Reap sandboxes/sidecars/networks the daemon still holds but nobody owns.

        :meth:`reap_idle` walks ``self._containers`` — **in-memory**. A worker
        restart empties that map, so every container, sidecar and network alive
        at that moment becomes permanently unreachable by the reaper. Measured
        on the prod DinD 2026-08-30: two exited sandboxes (4 days, **6 weeks**)
        and a ``sbxnet-*`` network with zero attached containers.

        So this sweep reads the DAEMON's state, not ours.

        **Only non-running things are touched.** One DinD can be shared by more
        than one worker process, so "not in my map ⇒ orphan" would kill another
        process's live work. A live sandbox is running by definition; an exited
        one is finished no matter who started it. Same for networks: zero
        attached containers means nothing is using it.

        Best-effort throughout — a sweep that raises would take down startup,
        and a leaked container is worth less than a booting worker.
        """
        code, out, _err = await self._docker(
            ["ps", "-a", "--format", "{{.Names}}\t{{.State}}"], timeout_s=_DOCKER_OP_TIMEOUT_S
        )
        if code == 0:
            for line in out.decode("utf-8", errors="replace").splitlines():
                name, _, state = line.partition("\t")
                if not name.startswith(_CONTAINER_PREFIX):
                    continue
                if state.strip().lower() in {"running", "true"}:
                    continue
                await self._docker(["rm", "-f", name], timeout_s=_DOCKER_OP_TIMEOUT_S)
                logger.info("sandbox_orphan_reaped", container=name)

        code, out, _err = await self._docker(
            ["network", "ls", "--format", "{{.Name}}"], timeout_s=_DOCKER_OP_TIMEOUT_S
        )
        if code != 0:
            return
        for name in out.decode("utf-8", errors="replace").splitlines():
            if not name.startswith(_NETWORK_PREFIX):
                continue
            ncode, nout, _ = await self._docker(
                ["network", "inspect", "-f", "{{len .Containers}}", name],
                timeout_s=_DOCKER_OP_TIMEOUT_S,
            )
            if ncode != 0 or nout.decode("utf-8", errors="replace").strip() not in {"0", ""}:
                continue
            await self._docker(["network", "rm", name], timeout_s=_DOCKER_OP_TIMEOUT_S)
            logger.info("sandbox_orphan_network_reaped", network=name)

    async def reap_idle(self) -> None:
        now = time.monotonic()
        stale = [
            pid
            for pid, entry in list(self._containers.items())
            if now - entry.last_used >= self._idle_reap_seconds
        ]
        for pid in stale:
            await self.release(pid)
