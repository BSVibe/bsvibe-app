"""A Claude Code session has limits INSIDE it, not only after it (#1104, #1114).

Measured 2026-09-30: run ``92b76fba``'s Claude Code task finished ``done`` with 3,055,575 input
tokens, and only THEN crossed ``agent_max_run_tokens`` (2M). The ceiling had not stopped a
runaway; it threw away finished work. Two causes, both in this module:

* the unit — ``input + cache_creation + cache_read`` summed at equal weight. Claude Code
  re-reads the whole conversation from cache on every turn, so the raw sum grows
  super-linearly in turns while the bill (cache reads ≈ 1/10 of input) does not;
* the moment — usage was read only off the terminal ``result`` event, after the session ended.

And there was no ``--max-turns``: the only in-session bound was the 2-hour timeout.

The event shapes below are copied from the real CLI (2.1.286, measured 2026-10-01): every
content block of one assistant message repeats that message's ``usage`` under the same
``message.id``; cache writes are 1-hour TTL (``ephemeral_1h_input_tokens``); and a session that
hits ``--max-turns`` ends with ``result/error_max_turns`` and **exit 1**.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.executors.worker import claude_code as claude_mod
from backend.executors.worker.claude_code import ClaudeCodeExecutor, _claude_extract_usage
from tests.executors.worker.test_claude_code import (
    _agent_ctx,
    _drain,
    _FakeProcess,
    _patch_subprocess,
)


def _line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")


def _assistant_usage(
    msg_id: str, *, inp: int, write_1h: int, read: int, out: int = 1, block: str = "tool_use"
) -> bytes:
    return _line(
        {
            "type": "assistant",
            "message": {
                "id": msg_id,
                "content": [{"type": block}],
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": write_1h,
                    "cache_read_input_tokens": read,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": write_1h,
                    },
                    "output_tokens": out,
                },
            },
        }
    )


def _spy_kill(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    killed: list[Any] = []

    def _kill(p: Any) -> None:
        killed.append(p)
        p.kill()

    monkeypatch.setattr(claude_mod, "_kill_process_group", _kill)
    return killed


# ── The unit: tokens weighted by what they cost ──────────────────────────────


def test_cache_reads_weigh_a_tenth_and_1h_cache_writes_weigh_double() -> None:
    usage = _claude_extract_usage(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 17,
                "cache_creation_input_tokens": 7473,
                "cache_read_input_tokens": 34881,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 7473,
                    "ephemeral_5m_input_tokens": 0,
                },
                "output_tokens": 233,
            },
        }
    )
    # 17 + 7473×2 + ceil(34881×0.1)
    assert usage == (17 + 14946 + 3489, 233)


def test_cache_writes_without_a_ttl_split_weigh_as_5m_writes() -> None:
    usage = _claude_extract_usage(
        {
            "type": "result",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 5,
            },
        }
    )
    assert usage == (10 + 125 + 100, 5)


# ── --max-turns ──────────────────────────────────────────────────────────────


async def test_an_agent_session_carries_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_subprocess(monkeypatch, _FakeProcess(stdout_lines=[]))

    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(max_turns="40")))

    argv = calls[0]
    assert argv[argv.index("--max-turns") + 1] == "40"


async def test_no_max_turns_means_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a task from a backend that sends none keeps today's invocation."""
    calls = _patch_subprocess(monkeypatch, _FakeProcess(stdout_lines=[]))

    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    assert "--max-turns" not in calls[0]


async def test_reaching_max_turns_is_a_finished_turn_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI exits 1 on ``error_max_turns``. Read as a failure, the adapter would RETRY the
    round — a fresh session repeating the work the limit just bounded. The agent's work is
    already in the run's worktree (it acts through BSVibe's tools), so the round ends normally
    and verification judges it."""
    result = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "num_turns": 41,
        "usage": {
            "input_tokens": 17,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 233,
        },
    }
    proc = _FakeProcess(stdout_lines=[_line(result)], returncode=1)
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(max_turns=40)))

    assert chunks[-1].done is True
    assert chunks[-1].error is None
    assert (chunks[-1].usage_prompt_tokens, chunks[-1].usage_completion_tokens) == (17, 233)


# ── The moment: the budget is enforced while the session runs ───────────────


async def test_the_session_is_killed_when_its_token_budget_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = _spy_kill(monkeypatch)
    # Each message weighs 100 + 0 + ceil(9000×0.1) + 1 = 1001.
    lines = [
        _assistant_usage("m1", inp=100, write_1h=0, read=9000),
        _assistant_usage("m2", inp=100, write_1h=0, read=9000),
        _assistant_usage("m3", inp=100, write_1h=0, read=9000),
    ]
    proc = _FakeProcess(stdout_lines=lines, returncode=0)
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(token_budget="2000")))

    assert killed == [proc], "the session must be killed, not left to finish"
    assert proc.stdout._lines, "nothing after the breach may be read"
    terminal = chunks[-1]
    assert terminal.done is True
    # Ends the round like a finished turn: a failure would be retried. The run's own ceiling
    # then sees the usage and stops on its Decision.
    assert terminal.error is None
    assert terminal.usage_prompt_tokens + terminal.usage_completion_tokens == 2002


async def test_repeated_blocks_of_one_message_are_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every content block repeats its message's usage (measured). Summing them would kill a
    session at half its budget."""
    killed = _spy_kill(monkeypatch)
    lines = [
        _assistant_usage("m1", inp=100, write_1h=0, read=9000, block="thinking"),
        _assistant_usage("m1", inp=100, write_1h=0, read=9000, block="tool_use"),
    ]
    proc = _FakeProcess(stdout_lines=lines, returncode=0)
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(token_budget=1500)))

    assert killed == []
    assert chunks[-1].done is True and chunks[-1].error is None


async def test_no_budget_means_no_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the same stream without a budget runs to its end."""
    killed = _spy_kill(monkeypatch)
    lines = [_assistant_usage(f"m{i}", inp=100, write_1h=0, read=9000) for i in range(3)]
    _patch_subprocess(monkeypatch, _FakeProcess(stdout_lines=lines, returncode=0))

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    assert killed == []
    assert chunks[-1].done is True and chunks[-1].error is None
