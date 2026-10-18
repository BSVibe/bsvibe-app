"""#949 item 4 — verification's LLM calls go to the JUDGE route, not the run's act adapter.

``RunOrchestrator._verifier`` built ``VerificationService(llm=self._llm)`` — the act adapter,
bound to the run (``run_id``, ``repo_url``, execution target, the run's worktree as cwd) with
the act caller's hour-long timeout. So for an executor account the acceptance judge's
chat-shaped completion was dispatched as a run-bound agentic task. ``resolve_judge_llm``
(caller ``workflow.judge``: 300 s, no run binding) existed and was public, and nothing in
production called it.

Now the loop factory resolves it and the verifier uses it. When it resolves nothing the
verifier keeps the act LLM — exactly today's behaviour — rather than turning every such
workspace's verification into a human-review Decision.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from typing import Any

import pytest


class _Llm:
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, **kw: Any) -> Any:  # pragma: no cover — never called here
        raise AssertionError


def _orch(**kw: Any) -> Any:
    from backend.workflow.application.agent_loop import RunOrchestrator
    from backend.workflow.infrastructure.sandbox import NoopSandboxManager

    return RunOrchestrator(session=object(), sandbox_manager=NoopSandboxManager(), **kw)  # type: ignore[arg-type]


def test_the_verifier_uses_the_judge_llm() -> None:
    act, judge = _Llm("act"), _Llm("judge")

    assert _orch(llm=act, judge_llm=judge)._verifier()._llm is judge


def test_without_a_judge_the_verifier_keeps_the_act_llm() -> None:
    """Control — today's behaviour, byte for byte."""
    act = _Llm("act")

    assert _orch(llm=act)._verifier()._llm is act
    assert _orch(llm=act, judge_llm=None)._verifier()._llm is act


@pytest.mark.asyncio
async def test_the_judge_resolution_threads_the_session_factory(monkeypatch) -> None:
    """Without it an executor judge polls on the run's bound session and holds a pooled
    connection idle across the turn — the #632/#686 outage shape the act turn already avoids."""
    from backend.workflow.application.runtime import account_resolution

    seen: dict[str, Any] = {}

    async def _resolve(session: Any, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(adapter=object())

    monkeypatch.setattr(account_resolution, "resolve_via_caller", _resolve)
    factory = object()
    run = SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4())

    llm = await account_resolution.resolve_judge_llm(
        object(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
    )

    assert llm is not None
    assert seen["caller_id"] == "workflow.judge"
    assert seen["session_factory"] is factory
    assert "run_id" not in seen, "the judge must not be bound to the run"


def test_the_loop_factory_resolves_the_judge_and_hands_it_over() -> None:
    """The factory is too dependency-heavy to stand up; unmeasured, the wiring breaks silently
    (verification just keeps running on the act adapter)."""
    from backend.workflow.application.runtime import agent_runtime

    src = inspect.getsource(agent_runtime)
    assert "await resolve_judge_llm(" in src
    assert "session_factory=session_factory" in src[src.index("await resolve_judge_llm(") :][:400]
    assert "judge_llm=judge_llm" in src
