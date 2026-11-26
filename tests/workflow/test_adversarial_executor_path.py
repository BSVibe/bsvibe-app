"""The adversarial verifier through the REAL executor adapter (the production judge model).

Live (a834f164, 9a4780c9): the verifier handed ``tools=`` to the executor adapter, which took it for
an agentic task, wanted a run-scoped MCP work-tool token and refused. Let through, those tools
would have worked on the real sandbox tree and bypassed the copy. The harness now drives the tool
loop itself over plain chat turns. Only the CLI transport (``dispatch.await_completion``) is
faked; the adapter, the loop adapter and the verifier are the real ones.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from backend.config import get_settings
from backend.dispatch.adapter import ExecutorAdapter
from backend.executors import dispatch
from backend.identity import oauth_service
from backend.workflow.application.adversarial_verifier import SCRATCH_ROOT, run_adversarial_round
from backend.workflow.application.loop_llm import ResolverLoopLlm

from .._support import shared_file_sessionmaker
from ..dispatch.test_adapter import (
    _executor_account,
    _make_redis,
    _seed_worker,
    _StubCompletedTask,
)
from .test_adversarial_verifier import _AdvBox, _finding, _res

pytestmark = pytest.mark.asyncio


def _reply(obj: Any) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj)


async def _round_through_adapter(
    monkeypatch: pytest.MonkeyPatch, cli_replies: list[str], box: _AdvBox
) -> tuple[dict[str, Any], list[Any], list[dict[str, Any]]]:
    """Run one adversarial round whose judge is a real ExecutorAdapter over a fake CLI that
    answers ``cli_replies`` in order. Returns (blob, issued tokens, task prompts sent)."""
    workspace_id = uuid.uuid4()
    issued: list[Any] = []
    prompts: list[dict[str, Any]] = []
    queue = list(cli_replies)

    async def _spy_issue(*a: Any, **kw: Any) -> Any:
        issued.append(kw)
        raise AssertionError("the verifier must not be issued a run-scoped work-tool token")

    async def _fake_cli(*_a: Any, **kw: Any) -> _StubCompletedTask:
        prompts.append(dict(kw))
        return _StubCompletedTask(status="done", output=_reply(queue.pop(0)))

    monkeypatch.setattr(oauth_service, "issue_run_task_token", _spy_issue)
    monkeypatch.setattr(dispatch, "await_completion", _fake_cli)

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            worker = await _seed_worker(
                setup, workspace_id=workspace_id, capabilities=["claude_code"]
            )
            account = _executor_account(workspace_id, worker.id)
            setup.add(account)
            await setup.commit()
        async with sf() as adapter_session:
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=adapter_session,
                session_factory=sf,
                settings=get_settings(),
                redis=await _make_redis(),
                run_id=None,  # the verifier has no run-scoped task: agent mode would refuse
            )
            run = SimpleNamespace(
                id="run-1",
                payload={"intent_text": "do it"},
                usage_prompt_tokens=0,
                usage_completion_tokens=0,
            )

            async def release() -> None:
                return None

            blob = await run_adversarial_round(
                llm=ResolverLoopLlm(adapter=adapter),
                run=run,
                box=box,
                baseline="base123",
                surface="code",
                release=release,
            )
    return blob, issued, prompts


async def test_the_verifier_runs_a_full_loop_through_the_real_executor_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _AdvBox(pytest=_res(1, "failed"))
    blob, _, prompts = await _round_through_adapter(
        monkeypatch,
        [
            {"tool": "grep", "args": {"pattern": "cleanup"}},
            {"findings": [_finding()]},
            {"conflict": False, "why": "unrelated"},  # the separate intent judge call
        ],
        box,
    )

    assert blob["status"] == "done", blob  # no ExecutorAdapterUnavailable
    assert blob["confirmed"] == 1
    assert any(c.startswith("grep cleanup") for c in blob["checks"])  # the harness ran the tool
    assert len(prompts) == 3  # tool request → harness output → final answer → intent judge
    assert any("git" in c and "grep" in c for c in box.commands)


async def test_no_probe_through_the_real_adapter_is_asked_once_then_no_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob, _, prompts = await _round_through_adapter(
        monkeypatch, [{"findings": []}, {"findings": []}], _AdvBox()
    )

    assert blob["status"] == "no_probe", blob
    assert blob["probes"] == 0
    assert len(prompts) == 2  # the nudge went to the model once


async def test_probing_after_the_nudge_is_done_through_the_real_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob, _, prompts = await _round_through_adapter(
        monkeypatch,
        [
            {"findings": []},
            {"tool": "grep", "args": {"pattern": "x"}},
            {"findings": []},
        ],
        _AdvBox(),
    )

    assert blob["status"] == "done", blob
    assert blob["probes"] >= 1
    assert len(prompts) == 3


async def test_the_verifier_never_issues_an_executor_task_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _AdvBox()
    blob, issued, _ = await _round_through_adapter(
        monkeypatch,
        [
            {"tool": "shell_exec", "args": {"command": "echo probe-xyz"}},
            {"findings": []},
        ],
        box,
    )

    assert issued == []  # issue_run_task_token was never called
    assert blob["status"] == "done", blob
    probes = [c for c in box.commands if "probe-xyz" in c]
    assert len(probes) == 1
    # the shell request ran only inside the copy, never in the real workspace mount
    assert "bsvibe-adversarial" in probes[0]
    assert probes[0].startswith("cd /tmp/bsvibe-adversarial")


async def test_two_malformed_replies_make_the_round_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _AdvBox()
    blob, _, prompts = await _round_through_adapter(
        monkeypatch, ["I think it is fine, no JSON.", "still prose"], box
    )

    assert blob["status"] == "unavailable"
    assert blob["findings"] == []
    assert len(prompts) == 2  # one re-ask, then give up


async def test_one_malformed_reply_is_asked_again_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _AdvBox()
    blob, _, prompts = await _round_through_adapter(
        monkeypatch,
        ["oops not json", {"tool": "grep", "args": {"pattern": "x"}}, {"findings": []}],
        box,
    )

    assert blob["status"] == "done"
    assert len(prompts) == 3


async def test_the_production_verify_round_confirms_through_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round exactly as ``VerificationService.verify`` calls it: a real ``ExecutionRun`` row,
    the SAME unbound judge adapter (as ``resolve_judge_llm`` builds it) for the investigation and the intent judge, and the real
    ``_release_connection`` as ``release``. Run ``205fa140`` (#1469) failed its intent judge with
    ``ExecutorAdapterUnavailable`` only here."""
    from backend.workflow.application.verification_service import VerificationService
    from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    repro = {"kind": "test", "path": f"{SCRATCH_ROOT}/{str(run_id)[:12]}/test_bug.py"}
    replies = [
        {"tool": "grep", "args": {"pattern": "cleanup"}},
        {"findings": [_finding(repro)]},
        {"conflict": False, "why": "unrelated"},
    ]
    prompts: list[dict[str, Any]] = []

    async def _fake_cli(*_a: Any, **kw: Any) -> _StubCompletedTask:
        prompts.append(dict(kw))
        return _StubCompletedTask(status="done", output=_reply(replies.pop(0)))

    monkeypatch.setattr(dispatch, "await_completion", _fake_cli)

    async with shared_file_sessionmaker() as sf:
        async with sf() as setup:
            worker = await _seed_worker(
                setup, workspace_id=workspace_id, capabilities=["claude_code"]
            )
            account = _executor_account(workspace_id, worker.id)
            setup.add(account)
            setup.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    status=RunStatus.RUNNING,
                    payload={"intent_text": "do it"},
                )
            )
            await setup.commit()
        async with sf() as service_session:
            run = await service_session.get(ExecutionRun, run_id)
            assert run is not None
            adapter = ExecutorAdapter(
                account=account,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
                session=service_session,
                session_factory=sf,
                settings=get_settings(),
                redis=await _make_redis(),
                run_id=None,  # resolve_judge_llm: the judge route is NOT bound to the run
                yield_on_saturation=False,
            )
            llm = ResolverLoopLlm(adapter=adapter)
            service = VerificationService(session=service_session, llm=llm, verifier_llm=llm)
            blob = await run_adversarial_round(
                llm=service._verifier_llm,
                run=run,
                box=_AdvBox(pytest=_res(1, "failed")),
                baseline="base123",
                surface="code",
                release=lambda: service._release_connection(run),
            )

    assert blob["status"] == "done", blob
    reasons = [f.get("reason") for f in blob["findings"]]
    assert blob["confirmed"] == 1, reasons
    assert len(prompts) == 3
