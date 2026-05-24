"""The ``opencode`` executor — streams from the opencode CLI.

Runs ``opencode run --format json <prompt>`` as an async subprocess and parses
its flat JSONL event stream into :class:`ExecutionChunk`s (assistant text, then
a terminal ``done`` / ``error``). Adapted from BSGateway's proven
``worker/executors.py`` streamer, mirroring the worker's ``claude_code``
executor.

Robustness:

* per-line read deadline (``timeout_seconds``) → a terminal timeout error chunk,
  never a hung loop;
* non-zero exit / OS errors → a terminal error chunk (no crash);
* ``TimeoutError`` is caught BEFORE the ``OSError`` branch (``TimeoutError``
  subclasses ``OSError`` in 3.11) so the explicit timeout message is surfaced
  rather than an empty ``str(OSError())``.

Each task is its own ``opencode run`` process, so per-task workspace (``--dir``),
model (``-m``), and system prompt are naturally isolated. ``opencode run
--format json`` emits a JSONL event stream on stdout: ``step_start`` →
``text`` → ``step_finish``; the assistant's answer is the ``part.text`` of each
``text`` event. The prompt is the trailing positional argument; ``system`` is
injected via the ``OPENCODE_CONFIG_CONTENT`` env var (an inline JSON config
opencode merges with its global config), referencing a temp file removed when
the subprocess exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import structlog

from backend.executors.worker.executors import ExecutionChunk, sanitized_subprocess_env

logger = structlog.get_logger(__name__)


class OpenCodeExecutor:
    """Stream from ``opencode run --format json``."""

    def __init__(self, timeout_seconds: int = 3600) -> None:
        self._cmd = shutil.which("opencode") or "opencode"
        self._timeout = timeout_seconds

    def supported_task_types(self) -> list[str]:
        return ["opencode"]

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        workspace = context.get("workspace_dir") or "."
        system = context.get("system") or ""
        model = context.get("model") or None
        deadline = asyncio.get_event_loop().time() + self._timeout

        # Build the per-task inline config (system instructions). opencode
        # merges OPENCODE_CONFIG_CONTENT over its global config, so each
        # subprocess is isolated without touching disk config or the workspace.
        sys_path: str | None = None
        config: dict[str, Any] = {}
        if system:
            sys_path = _write_system_file(system)
            config["instructions"] = [sys_path]

        # Start from a sanitized env (no parent Claude-Code session leakage),
        # then layer opencode's per-task inline config on top.
        env = sanitized_subprocess_env()
        if config:
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)

        cmd_args = self._build_cmd(workspace, model, prompt)
        try:
            async for chunk in self._run(cmd_args, workspace, env, deadline):
                yield chunk
        finally:
            if sys_path:
                try:
                    os.unlink(sys_path)
                except OSError:  # pragma: no cover — best-effort cleanup
                    pass

    async def _run(
        self,
        cmd_args: list[str],
        workspace: str,
        env: dict[str, str],
        deadline: float,
    ) -> AsyncIterator[ExecutionChunk]:
        process: asyncio.subprocess.Process | None = None
        stderr_buf: list[str] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=workspace,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            assert process.stdout is not None
            assert process.stderr is not None

            stderr_task = asyncio.create_task(_drain(process.stderr, stderr_buf))
            try:
                async for line in _aiter_lines(process.stdout, deadline):
                    parsed = _safe_json(line)
                    if parsed is None:
                        continue
                    delta = _opencode_extract_delta(parsed)
                    if delta:
                        yield ExecutionChunk(delta=delta, raw=parsed)
            finally:
                rc = await asyncio.wait_for(
                    process.wait(),
                    timeout=max(0.1, deadline - asyncio.get_event_loop().time()),
                )
                await stderr_task
            err_text = "".join(stderr_buf)
            if rc != 0:
                yield ExecutionChunk(done=True, error=err_text or f"exit {rc}")
            else:
                yield ExecutionChunk(done=True)
        except TimeoutError:
            # ``TimeoutError`` is a subclass of ``OSError`` (3.11) — handle it
            # BEFORE the ``OSError`` branch so the explicit timeout message is
            # surfaced rather than the empty ``str(OSError())`` below.
            yield ExecutionChunk(done=True, error=f"Execution timed out after {self._timeout}s")
        except (FileNotFoundError, PermissionError, OSError) as exc:
            yield ExecutionChunk(done=True, error=str(exc))
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:  # pragma: no cover — race on shutdown
                    pass

    def _build_cmd(self, workspace: str, model: str | None, prompt: str) -> list[str]:
        cmd_args = [
            self._cmd,
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "--dir",
            workspace,
        ]
        if model:
            cmd_args += ["-m", model]
        cmd_args.append(prompt)
        return cmd_args


# ── Stream parsing helpers ────────────────────────────────────────────────────


def _opencode_extract_delta(event: dict[str, Any]) -> str:
    """Pull assistant text from an ``opencode run --format json`` event.

    ``opencode run`` emits a flat JSONL stream of ``{"type": ..., "part": {...}}``
    records: ``step_start`` → ``text`` → ``step_finish``. The assistant's answer
    is the ``part.text`` of each ``text`` event; tool / step events carry no
    user-facing text.
    """
    if event.get("type") == "text":
        part = event.get("part") or {}
        if isinstance(part, dict):
            text = part.get("text") or ""
            return text if isinstance(text, str) else ""
    return ""


def _write_system_file(system: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
    try:
        tmp.write(system)
    finally:
        tmp.close()
    return tmp.name


async def _aiter_lines(stream: asyncio.StreamReader, deadline: float) -> AsyncIterator[str]:
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError
        line = await asyncio.wait_for(stream.readline(), timeout=remaining)
        if not line:
            return
        yield line.decode("utf-8", errors="replace").rstrip("\n")


async def _drain(stream: asyncio.StreamReader, buf: list[str]) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        buf.append(chunk.decode("utf-8", errors="replace"))


def _safe_json(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


__all__ = ["OpenCodeExecutor"]
