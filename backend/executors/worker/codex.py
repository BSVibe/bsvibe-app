"""The ``codex`` executor — streams from the OpenAI Codex CLI.

Runs ``codex exec --json`` as an async subprocess, feeds the prompt on stdin,
and parses its item-based JSONL event stream into :class:`ExecutionChunk`s
(assistant text, then a terminal ``done`` / ``error``). Adapted from
BSGateway's proven ``worker/executors.py`` streamer, mirroring the worker's
``claude_code`` executor.

Robustness:

* per-line read deadline (``timeout_seconds``) → a terminal timeout error chunk,
  never a hung loop;
* non-zero exit / OS errors → a terminal error chunk (no crash);
* ``TimeoutError`` is caught BEFORE the ``OSError`` branch (``TimeoutError``
  subclasses ``OSError`` in 3.11) so the explicit timeout message is surfaced
  rather than an empty ``str(OSError())``.

Flags track codex-cli's current contract:

* ``exec --json`` — non-interactive run with the JSONL event stream;
* ``--skip-git-repo-check`` — the per-task workspace is a fresh empty temp dir
  (no git repo, untrusted), which codex otherwise refuses to run in;
* ``--sandbox read-only`` + ``--disable <feature>`` × N + ``web_search="disabled"`` — codex's
  own tools are off (#1000); an agent run acts only through BSVibe's MCP tools, named in a
  per-run ``CODEX_HOME/config.toml`` (``enabled_tools``) with the token in an env var;
* ``--config model_instructions_file=<path>`` — the system-message override
  (the old ``experimental_instructions_file`` key silently drops it);
* ``--model`` — per-run model override.

The prompt is fed on stdin (codex reads stdin when no positional prompt is
given). ``workspace_dir`` becomes the subprocess cwd; the system message (if
any) is written to a temp file removed when the subprocess exits.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from backend.executors.worker.config import WorkerSettings
from backend.executors.worker.executors import (
    ExecutionChunk,
    _kill_process_group,
    sanitized_subprocess_env,
    usage_int,
)

logger = structlog.get_logger(__name__)

# ``codex exec --json`` emits one JSON event per line, and a single line can
# carry full file contents / diffs / reasoning — easily past asyncio's default
# 64 KiB StreamReader limit. Without a raised limit ``readline()`` raises
# ``LimitOverrunError`` ("Separator is found, but chunk is longer than limit")
# mid-run and the task dies. 16 MiB comfortably covers a large multi-file turn.
_STREAM_LIMIT = 16 * 1024 * 1024


#: Built-ins switched off for EVERY run (#1000, measured on codex-cli 0.163.0 by capturing the
#: request body sent to the model provider). With these off, gpt-5.1-codex keeps one built-in:
#: ``request_user_input`` (no file, no command). Removing one from this list hands a native
#: tool back; ``view_image`` is here because 0.163 made it a flag (on 0.130 it was not).
_DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool",
    "multi_agent",
    "goals",
    "view_image",
    "browser_use",
    "computer_use",
    "in_app_browser",
    "image_generation",
)

#: Models measured WITHOUT ``apply_patch`` once the features above are off. On gpt-5.5 it
#: stays even with all 118 flags disabled — a native write tool, so that family is refused
#: for an agent run rather than trusted.
_AGENT_MODELS: frozenset[str] = frozenset({"gpt-5.1-codex", "gpt-5.1", "o3"})
_DEFAULT_AGENT_MODEL = "gpt-5.1-codex"

#: The env var codex reads the MCP bearer token from — never argv, never a file.
_TOKEN_ENV = "BSVIBE_MCP_TOKEN"  # noqa: S105 — an env var NAME; the value is never here

#: Item types that mean codex acted with its OWN hands. Switched off by the flags above; seen
#: anyway (a CLI upgrade reviving a tool), the session ends instead of the work being kept.
_NATIVE_ACTION_ITEMS: frozenset[str] = frozenset({"command_execution", "file_change"})


class _Refused(Exception):
    """A run that cannot be shaped — the message is the reason."""


class CodexExecutor:
    """Stream from ``codex exec --json``."""

    def __init__(self, timeout_seconds: int = 3600, *, auth_file: str | None = None) -> None:
        self._cmd = shutil.which("codex") or "codex"
        self._timeout = timeout_seconds
        self._auth_file = auth_file

    async def execute(self, prompt: str, context: dict[str, Any]) -> AsyncIterator[ExecutionChunk]:
        workspace = context.get("workspace_dir") or "."
        system = context.get("system") or ""
        # Absent key → agent run (a task from an older backend carries no flag).
        agentic = context.get("agentic", True) is not False
        try:
            model = _model_for(context.get("model") or None, agentic=agentic)
            surface = _plan_surface(context) if agentic else None
            auth = _login_file(self._auth_file)
        except _Refused as exc:
            yield ExecutionChunk(done=True, error=str(exc))
            return
        deadline = asyncio.get_event_loop().time() + self._timeout

        home = tempfile.mkdtemp(prefix="bsvibe-codex-")
        sys_path: str | None = _write_system_file(system) if system else None
        try:
            env = _prepare_home(home, auth, surface)
            cmd_args = self._build_cmd(sys_path, model)
            async for chunk in self._run(cmd_args, prompt, workspace, deadline, env):
                yield chunk
        finally:
            # The home holds a copy of the operator's login — never left behind.
            shutil.rmtree(home, ignore_errors=True)
            if sys_path:
                try:
                    os.unlink(sys_path)
                except OSError:  # pragma: no cover — best-effort cleanup
                    pass

    async def _run(
        self,
        cmd_args: list[str],
        prompt: str,
        workspace: str,
        deadline: float,
        env: dict[str, str],
    ) -> AsyncIterator[ExecutionChunk]:
        process: asyncio.subprocess.Process | None = None
        stderr_buf: list[str] = []
        try:
            # Lift E15 — ``start_new_session=True`` so we can group-kill on
            # cancel (see opencode.py for the dogfood story).
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            stderr_task = asyncio.create_task(_drain(process.stderr, stderr_buf))
            seen = _Seen()
            try:
                async for delta in _stream(process, deadline, seen):
                    yield ExecutionChunk(delta=delta)
            except asyncio.CancelledError:
                # Lift E15 — kill the process GROUP before the inner
                # ``finally``'s ``process.wait()`` blocks for the full
                # per-task deadline. See opencode._run for the diagnosis.
                logger.info(
                    "worker_subprocess_terminate_sent",
                    pid=process.pid,
                    executor="codex",
                    reason="cancelled",
                )
                _kill_process_group(process)
                raise
            finally:
                rc = await asyncio.wait_for(
                    process.wait(),
                    timeout=max(0.1, deadline - asyncio.get_event_loop().time()),
                )
                await stderr_task
            # Usage rides the terminal chunk on failure too: a turn that burned
            # tokens and then exited non-zero still spent the founder's budget.
            yield seen.terminal(rc, "".join(stderr_buf))
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
                    _kill_process_group(process)
                    await process.wait()
                    logger.info("worker_subprocess_killed", pid=process.pid, executor="codex")
                except ProcessLookupError:  # pragma: no cover — race on shutdown
                    pass

    def _build_cmd(self, sys_path: str | None, model: str | None) -> list[str]:
        # The worker's sandbox cwd is not a git repo and not in codex's trusted-projects
        # list; without --skip-git-repo-check the CLI refuses ("Not inside a trusted
        # directory"). ``read-only``: with its own tools off nothing of codex's writes —
        # BSVibe's tools act server-side.
        cmd_args = [
            self._cmd,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
        ]
        for feature in _DISABLED_FEATURES:
            cmd_args += ["--disable", feature]
        cmd_args += ["-c", 'web_search="disabled"']
        if sys_path:
            cmd_args += ["--config", f"model_instructions_file={sys_path}"]
        if model:
            cmd_args += ["--model", model]
        return cmd_args


def _model_for(requested: str | None, *, agentic: bool) -> str | None:
    """The model this run may use. An agent run only gets one measured without a native
    write tool; a chat turn has no tools to worry about and keeps whatever it asked for."""
    if not agentic:
        return requested
    if requested is None:
        return _DEFAULT_AGENT_MODEL
    if requested not in _AGENT_MODELS:
        raise _Refused(
            f"codex model {requested!r} is not one measured without its own apply_patch "
            f"write tool; an agent run may use {sorted(_AGENT_MODELS)}"
        )
    return requested


def _plan_surface(context: dict[str, Any]) -> tuple[str, str, list[str]]:
    """``(url, token, tool names)`` of BSVibe's MCP surface — or a refusal (#1000).

    An agent run acts through BSVibe's tools only; a task carrying no surface is version skew
    or a malformed dispatch, and is never downgraded to codex's own hands."""
    mcp_config = str(context.get("mcp_config") or "")
    allowed = [str(t) for t in (context.get("allowed_tools") or [])]
    refused = _Refused(
        "aborted: an agent run must act through BSVibe's tools, and this task carries no "
        "usable MCP surface. BSVibe relays every execution — there is no local agent run."
    )
    try:
        servers = json.loads(mcp_config).get("mcpServers") if mcp_config else None
    except (TypeError, ValueError, AttributeError):
        servers = None
    entry = servers.get("bsvibe") if isinstance(servers, dict) else None
    if not (isinstance(entry, dict) and allowed):
        raise refused
    url = entry.get("url")
    auth = (entry.get("headers") or {}).get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if isinstance(auth, str) else ""
    if not (isinstance(url, str) and url and token):
        raise refused
    names = [t.split("__")[-1] if t.startswith("mcp__") else t for t in allowed]
    return url, token, names


def _login_file(configured: str | None) -> Path:
    path = Path(
        configured or WorkerSettings().codex_auth_file or Path.home() / ".codex" / "auth.json"
    ).expanduser()
    if not path.is_file():
        raise _Refused(
            f"codex is not logged in on this worker (no {path}). Run `codex login` as the "
            "worker's user, or point BSVIBE_WORKER_CODEX_AUTH_FILE at its auth.json."
        )
    return path


def _prepare_home(
    home: str, auth: Path, surface: tuple[str, str, list[str]] | None
) -> dict[str, str]:
    """Fill this run's own ``CODEX_HOME``; return the subprocess environment.

    Only the login is copied in (0600). The config names BSVibe's server and EXACTLY the
    dispatched tools under ``enabled_tools`` — the key codex honours; a misspelling is
    silently ignored and every advertised tool would reach the model (measured). The token
    stays in the environment the config points at, so it is on no argv and in no file.
    """
    os.chmod(home, 0o700)
    target = Path(home) / "auth.json"
    shutil.copyfile(auth, target)
    os.chmod(target, 0o600)
    env = {**sanitized_subprocess_env(), "CODEX_HOME": home}
    lines: list[str] = []
    if surface is not None:
        url, token, names = surface
        lines += [
            "[mcp_servers.bsvibe]",
            f"url = {json.dumps(url)}",
            f'bearer_token_env_var = "{_TOKEN_ENV}"',
            f"enabled_tools = {json.dumps(names)}",
        ]
        env[_TOKEN_ENV] = token
    (Path(home) / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


@dataclass
class _Seen:
    """What the stream told us besides text: the turn's usage (last value wins — codex
    reports one cumulative usage per turn) and a native action, if codex took one."""

    usage: tuple[int, int] = (0, 0)
    native: str | None = None

    def terminal(self, rc: int, err_text: str) -> ExecutionChunk:
        if self.native is not None:
            logger.warning("codex_native_action_refused", item_type=self.native)
            error: str | None = (
                f"aborted: codex acted with its own tools ({self.native}) although they "
                "were switched off — BSVibe's tools are the only sanctioned hands"
            )
        else:
            error = (err_text or f"exit {rc}") if rc != 0 else None
        return ExecutionChunk(
            done=True,
            error=error,
            usage_prompt_tokens=self.usage[0],
            usage_completion_tokens=self.usage[1],
        )


async def _stream(
    process: asyncio.subprocess.Process, deadline: float, seen: _Seen
) -> AsyncIterator[str]:
    """The assistant text of the run; stops (and kills the CLI) on a native action."""
    assert process.stdout is not None
    async for line in _aiter_lines(process.stdout, deadline):
        parsed = _safe_json(line)
        if parsed is None:
            continue
        seen.native = _native_action(parsed)
        if seen.native is not None:
            _kill_process_group(process)
            return
        reported = _codex_extract_usage(parsed)
        if reported is not None:
            seen.usage = reported
        delta = _codex_extract_delta(parsed)
        if delta:
            yield delta


def _native_action(event: dict[str, Any]) -> str | None:
    """The item type when codex reports acting with its own tools, else ``None``."""
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in _NATIVE_ACTION_ITEMS:
        return str(item.get("type"))
    return None


# ── Stream parsing helpers ────────────────────────────────────────────────────


def _codex_extract_delta(event: dict[str, Any]) -> str:
    """Pull assistant text from a ``codex exec --json`` JSONL event.

    codex-cli emits an item-based stream: ``thread.started`` → ``turn.started``
    → ``item.*`` → ``turn.completed``. The assistant's answer arrives whole as
    ``{"type": "item.completed", "item": {"type": "agent_message", "text": ...}}``
    — there is no token-level delta event, so we surface the text from the
    completed ``agent_message`` item only.
    """
    if event.get("type") == "item.completed":
        item = event.get("item") or {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text") or ""
            return text if isinstance(text, str) else ""
    return ""


def _codex_extract_usage(event: dict[str, Any]) -> tuple[int, int] | None:
    """Pull ``(prompt, completion)`` token counts off ``turn.completed``.

    codex closes each turn with ``{"type": "turn.completed", "usage": {...}}``.
    Cached input is still input the account is billed for, so it counts toward
    the prompt total.

    Returns ``None`` — never ``(0, 0)`` — for an event without usage, so a turn
    that reported nothing stays distinguishable from a turn that cost nothing.
    """
    if event.get("type") != "turn.completed":
        return None
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage_int(usage.get("input_tokens")) + usage_int(usage.get("cached_input_tokens"))
    return prompt, usage_int(usage.get("output_tokens"))


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


__all__ = ["CodexExecutor"]
