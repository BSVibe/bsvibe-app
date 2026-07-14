"""The executor acts through BSVibe's tools, or it does not act at all (T2).

BSVibe's first principle: an executor account and a LiteLLM account behave IDENTICALLY
through ``chat()``. The founder's clarification: the executor is the user's **LLM client**,
not an execution environment — state lives on the server, and an agent acts on it through
BSVibe's tools (T1/T1b), executed server-side.

So ``chat(tools=[...])`` means one thing on both transports:

    LiteLLM  → native tool_calls, executed in-process by the ToolRegistry
    Executor → the CLI is handed the SAME tools over MCP (run-scoped token), and its own
               native tools are taken away

An executor CLI that cannot be given BSVibe's tools cannot honour the contract. It is then
**refused, loudly** — never quietly run in the old shape. Silent divergence is what produced
the whole audit: an agent inventing code in an empty temp dir, a >256 KB file zeroed, a
deletion losing the entire result. "Which account did I route?" must not change what the
product does.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.dispatch.adapter import ExecutorAdapter, supports_remote_tools

pytestmark = pytest.mark.asyncio


def test_claude_code_can_carry_bsvibes_tools() -> None:
    """Verified against the real CLI: it accepts an HTTP MCP server with auth headers and
    restricts the model to exactly the tools we allow."""
    assert supports_remote_tools("claude_code") is True


@pytest.mark.parametrize("executor", ["codex", "opencode"])
def test_the_others_cannot_yet(executor: str) -> None:
    """Until each one's MCP shape is verified against the real binary — not guessed — it
    cannot carry BSVibe's tools. Guessing a CLI's contract is how wrappers rot."""
    assert supports_remote_tools(executor) is False


class _Account:
    def __init__(self, executor_type: str) -> None:
        self.provider = "executor"
        self.litellm_model = "sonnet"
        self.extra_params = {"executor_type": executor_type, "worker_id": str(uuid.uuid4())}


def _adapter(executor_type: str, *, redis: Any = object()) -> ExecutorAdapter:
    from backend.config import get_settings

    return ExecutorAdapter(
        account=_Account(executor_type),  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        model_account_id=uuid.uuid4(),
        session=None,  # type: ignore[arg-type]
        settings=get_settings(),
        redis=redis,
    )


@pytest.mark.parametrize("executor", ["codex", "opencode"])
async def test_agentic_work_on_an_unsupported_executor_is_refused(executor: str) -> None:
    """The founder routed a coding run to an executor that cannot be given BSVibe's tools.

    It is NOT run in the old shape (its own local tools, in its own temp dir, scraped back).
    That path is what let an agent invent a codebase and ship it. The run fails with a reason
    the founder can act on: route the work elsewhere."""
    from backend.dispatch.adapter import ExecutorAdapterUnavailable

    with pytest.raises(ExecutorAdapterUnavailable, match="cannot use BSVibe's tools"):
        await _adapter(executor).chat(
            system="",
            messages=[{"role": "user", "content": "add a mean() helper"}],
            tools=[{"type": "function", "function": {"name": "file_write"}}],
        )


@pytest.mark.parametrize("executor", ["codex", "opencode"])
async def test_a_chat_turn_on_an_unsupported_executor_still_works(executor: str) -> None:
    """Refusal is scoped to TOOLS. A chat turn (frame, judge, an answer) needs none, so those
    executors keep serving them — the founder's routing is only narrowed where it must be."""
    from backend.dispatch.adapter import ExecutorAdapterUnavailable

    # No redis → the dispatch path raises its usual unavailable error, NOT the tool refusal.
    with pytest.raises(ExecutorAdapterUnavailable) as exc:
        await _adapter(executor, redis=None).chat(
            system="", messages=[{"role": "user", "content": "what is 6*7?"}], tools=None
        )

    assert "cannot use BSVibe's tools" not in str(exc.value)
