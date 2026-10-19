"""#1000 — codex acts through BSVibe's MCP tools only, like claude_code and opencode.

Measured against codex-cli **0.163.0** by capturing the request body it sends its model
provider (no model tokens), 2026-10-19:

* ``--disable`` shell_tool · multi_agent · goals · view_image · browser_use · computer_use ·
  in_app_browser · image_generation, plus ``web_search="disabled"``, leaves ONE built-in on
  gpt-5.1-codex: ``request_user_input`` (no file, no command). ``view_image`` became a feature
  flag after 0.130 — the reason codex was refused on 2026-09-19 is gone.
* ``apply_patch`` (a WRITE tool) stays on gpt-5.5 even with all 118 feature flags off — so an
  agent run is pinned to a model measured without it.
* ``[mcp_servers.bsvibe] url · bearer_token_env_var · enabled_tools``: the server got
  ``Authorization: Bearer <token>`` from the env var (never argv, never a file), and
  ``enabled_tools`` dropped a tool the server advertised that was not on the list. 🚨 A
  misspelt ``enabled_toolz`` is silently ignored and lets every advertised tool through.
* A ``CODEX_HOME`` holding only a copy of ``auth.json`` is logged in, and carries none of the
  host's ``config.toml`` (whose personal MCP servers and memories DB codex otherwise attaches).
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from backend.executors.worker.codex import CodexExecutor
from tests.executors.worker.test_codex import _agent_message_line, _drain, _FakeProcess

pytestmark = pytest.mark.asyncio

_TOKEN = "run-scoped-SECRET-token"
_SURFACE = {
    "mcp_config": json.dumps(
        {
            "mcpServers": {
                "bsvibe": {
                    "type": "http",
                    "url": "https://api.bsvibe.dev/mcp/",
                    "headers": {"Authorization": f"Bearer {_TOKEN}"},
                }
            }
        }
    ),
    "allowed_tools": ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_shell_exec"],
}


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    p = tmp_path / "host-auth.json"
    p.write_text('{"tokens": "chatgpt-login"}')
    return p


class _Spawn:
    """Records what the CLI was started with — including the files of its CODEX_HOME, read
    AT SPAWN, because the executor removes that home when the run ends."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.config: dict[str, Any] = {}
        self.config_text = ""
        self.auth = ""
        self.auth_mode = 0
        self.calls = 0


def _patch(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> _Spawn:
    import asyncio

    spawn = _Spawn()

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawn.calls += 1
        spawn.argv = [str(a) for a in args]
        spawn.env = dict(kwargs.get("env") or {})
        home = Path(spawn.env.get("CODEX_HOME", "/nonexistent"))
        if (home / "config.toml").exists():
            spawn.config_text = (home / "config.toml").read_text()
            spawn.config = tomllib.loads(spawn.config_text)
        if (home / "auth.json").exists():
            spawn.auth = (home / "auth.json").read_text()
            spawn.auth_mode = stat.S_IMODE(os.stat(home / "auth.json").st_mode)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return spawn


def _ok() -> _FakeProcess:
    return _FakeProcess(stdout_lines=[_agent_message_line("done")])


async def _run(auth: Path, context: dict[str, Any]) -> list[Any]:
    return await _drain(
        CodexExecutor(auth_file=str(auth)).execute("do it", {"workspace_dir": ".", **context})
    )


# --- the agent's surface is BSVibe's tools -----------------------------------------------------


async def test_an_agent_run_without_bsvibes_surface_is_refused(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    chunks = await _run(auth_file, {"agentic": True})

    assert spawn.calls == 0
    assert chunks[-1].done and "BSVibe" in (chunks[-1].error or "")


async def test_codex_own_tools_are_switched_off(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, **_SURFACE})

    for feature in (
        "shell_tool",
        "multi_agent",
        "goals",
        "view_image",
        "browser_use",
        "computer_use",
        "in_app_browser",
        "image_generation",
    ):
        i = spawn.argv.index(feature)
        assert spawn.argv[i - 1] == "--disable", feature
    assert 'web_search="disabled"' in spawn.argv
    assert spawn.argv[spawn.argv.index("--sandbox") + 1] == "read-only"


async def test_the_mcp_server_is_ours_with_exactly_the_dispatched_tools(
    monkeypatch, auth_file
) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, **_SURFACE})

    server = spawn.config["mcp_servers"]["bsvibe"]
    assert server["url"] == "https://api.bsvibe.dev/mcp/"
    # The key codex honours — a misspelling is silently ignored (measured) and every tool our
    # server advertises would reach the model.
    assert server["enabled_tools"] == ["bsvibe_work_file_read", "bsvibe_work_shell_exec"]
    assert server["bearer_token_env_var"] in spawn.env
    assert spawn.env[server["bearer_token_env_var"]] == _TOKEN


async def test_the_token_is_never_on_argv_or_on_disk(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, **_SURFACE})

    assert _TOKEN not in " ".join(spawn.argv)
    assert _TOKEN not in spawn.config_text


async def test_codex_runs_in_its_own_home_with_only_the_login(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, **_SURFACE})

    home = Path(spawn.env["CODEX_HOME"])
    assert home != Path.home() / ".codex"
    assert spawn.auth == auth_file.read_text()
    assert spawn.auth_mode == 0o600
    assert not home.exists(), "the per-run home (holding a login copy) must be removed"


async def test_a_missing_login_fails_loudly(monkeypatch, tmp_path) -> None:
    spawn = _patch(monkeypatch, _ok())

    chunks = await _run(tmp_path / "absent.json", {"agentic": True, **_SURFACE})

    assert spawn.calls == 0
    assert "codex login" in (chunks[-1].error or "")


# --- the model: only ones measured without a native write tool ----------------------------------


async def test_an_agent_run_defaults_to_a_model_without_apply_patch(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, **_SURFACE})

    assert spawn.argv[spawn.argv.index("--model") + 1] == "gpt-5.1-codex"


async def test_a_model_that_keeps_apply_patch_is_refused(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    chunks = await _run(auth_file, {"agentic": True, "model": "gpt-5.5", **_SURFACE})

    assert spawn.calls == 0
    assert "apply_patch" in (chunks[-1].error or "")


async def test_a_measured_model_is_used_as_asked(monkeypatch, auth_file) -> None:
    spawn = _patch(monkeypatch, _ok())

    await _run(auth_file, {"agentic": True, "model": "gpt-5.1", **_SURFACE})

    assert spawn.argv[spawn.argv.index("--model") + 1] == "gpt-5.1"


# --- belt and braces: a native action is stopped, not trusted away ------------------------------


@pytest.mark.parametrize("item_type", ["command_execution", "file_change"])
async def test_a_native_action_ends_the_session(monkeypatch, auth_file, item_type) -> None:
    """The flags are measured on one version; a CLI upgrade can bring a tool back. If codex
    acts with its own hands anyway, the session stops instead of the work being kept."""
    event = {"type": "item.started", "item": {"type": item_type, "command": "rm -rf /"}}
    proc = _FakeProcess(
        stdout_lines=[(json.dumps(event) + "\n").encode(), _agent_message_line("done")]
    )
    _patch(monkeypatch, proc)
    killed: list[Any] = []
    # Never let a fake process reach os.killpg — pid 0 would be the test runner's own group.
    monkeypatch.setattr(
        "backend.executors.worker.codex._kill_process_group", lambda p: killed.append(p)
    )

    chunks = await _run(auth_file, {"agentic": True, **_SURFACE})

    assert killed == [proc]
    assert chunks[-1].done
    assert item_type in (chunks[-1].error or "")
    assert not any(c.delta == "done" for c in chunks), "nothing after the native action is kept"


# --- a chat turn: no tools at all -----------------------------------------------------------------


async def test_a_chat_turn_runs_with_everything_off(monkeypatch, auth_file) -> None:
    """0.163 can turn its tools off (only ``request_user_input`` stays), so codex now serves
    chat turns like the other two instead of refusing them."""
    spawn = _patch(monkeypatch, _ok())

    chunks = await _run(auth_file, {"agentic": False})

    assert spawn.calls == 1
    assert "mcp_servers" not in spawn.config
    assert spawn.argv[spawn.argv.index("view_image") - 1] == "--disable"
    assert chunks[-1].error is None
