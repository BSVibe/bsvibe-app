"""Stage A replay over a past deliverable (``backend.workflow.application.adversarial_replay``).

Real git repositories (a base commit A, a later commit C on ``main``, and a diff made on top of
A); only the judge model and — in the scoring tests — Stage A itself are faked. One test drives a
whole replay through the real ExecutorAdapter path like ``test_adversarial_executor_path.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.config import get_settings
from backend.dispatch.adapter import ExecutorAdapter
from backend.executors import dispatch
from backend.identity import oauth_service
from backend.mcp.tools.adversarial_replay_tools import AdversarialReplayInput
from backend.workflow.application import adversarial_replay as replay
from backend.workflow.application.adversarial_replay import (
    CaseSpec,
    ReplayInput,
    ReplayRefused,
    run_replay,
)
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession

from .._support import shared_file_sessionmaker
from ..dispatch.test_adapter import (
    _executor_account,
    _make_redis,
    _seed_worker,
    _StubCompletedTask,
)

pytestmark = pytest.mark.asyncio


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        **{"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    }
    if date:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = date
    return subprocess.run(  # noqa: S603 — fixed git argv in a temp repo
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


class _Repo:
    def __init__(self, root: Path) -> None:
        self.path = root / "product"
        self.path.mkdir()
        _git(self.path, "init", "-b", "main")
        (self.path / "a.py").write_text("def f():\n    return 1\n")
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-m", "A", date="2026-01-01T00:00:00Z")
        self.a = _git(self.path, "rev-parse", "HEAD")
        (self.path / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        _git(self.path, "commit", "-am", "c", date="2026-03-01T00:00:00Z")
        self.c = _git(self.path, "rev-parse", "HEAD")
        # the deliverable's diff: made on top of A (not on C)
        _git(self.path, "checkout", "-q", "--detach", self.a)
        (self.path / "a.py").write_text("def f():\n    return 41\n")
        (self.path / "new.py").write_text("X = 1\n")
        _git(self.path, "add", "-A")
        self.diff = _git(self.path, "diff", "--cached", "--binary") + "\n"
        self.applied_a_py = "def f():\n    return 41\n"
        _git(self.path, "reset", "-q", "--hard", self.a)
        _git(self.path, "clean", "-fdq")
        _git(self.path, "checkout", "-q", "main")

    def input(self, **kw: Any) -> ReplayInput:
        base: dict[str, Any] = {
            "diff": self.diff,
            "diff_truncated": False,
            "recorded_base": None,
            "created_at": datetime(2026, 2, 1, tzinfo=UTC),
            "repo_dir": self.path,
        }
        return ReplayInput(**{**base, **kw})


@contextlib.asynccontextmanager
async def _noop_box(path: Path) -> AsyncIterator[NoopSandboxSession]:
    yield NoopSandboxSession(str(path))


def _loader(inputs: dict[uuid.UUID, ReplayInput]) -> Any:
    async def load(deliverable_id: uuid.UUID) -> ReplayInput:
        return inputs[deliverable_id]

    return load


def _blob(confirmed: int = 0) -> dict[str, Any]:
    findings = [
        {"claim": f"bug {i}", "file": "a.py", "line": 2, "confirmed": True, "reason": "ok"}
        for i in range(confirmed)
    ]
    return {
        "status": "done",
        "surface": "code",
        "probes": 1,
        "findings": findings,
        "confirmed": confirmed,
        "unconfirmed": 0,
        "checks": ["grep x"],
    }


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path)


async def test_stage_a_sees_the_tree_with_the_diff_applied_on_the_base_commit(
    repo: _Repo, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    async def stage_a(**kw: Any) -> dict[str, Any]:
        root = Path(kw["box"].workspace_mount)
        seen["a_py"] = (root / "a.py").read_text()
        seen["new_py"] = (root / "new.py").read_text()
        seen["head"] = _git(root, "rev-parse", "HEAD")
        seen["baseline"] = kw["baseline"]
        seen["root"] = root
        return _blob()

    did = uuid.uuid4()
    out = await run_replay(
        [CaseSpec(did)],
        load=_loader({did: repo.input()}),  # no recorded base: found by the index blobs
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )

    assert seen["a_py"] == repo.applied_a_py  # A's a.py with the diff, NOT main's later a.py
    assert seen["new_py"] == "X = 1\n"
    assert seen["head"] == seen["baseline"] == repo.a  # not C: the nearest matching commit
    assert out["results"][0]["status"] == "ran"
    assert out["results"][0]["runs"][0]["base_commit"] == repo.a
    # the product's own repository is untouched, and the tree is gone
    assert _git(repo.path, "rev-parse", "HEAD") == repo.c
    assert _git(repo.path, "status", "--porcelain") == ""
    assert not seen["root"].exists()


async def test_a_recorded_base_commit_is_used_as_is(repo: _Repo, tmp_path: Path) -> None:
    calls: list[str] = []

    async def stage_a(**kw: Any) -> dict[str, Any]:
        calls.append(kw["baseline"])
        return _blob()

    did = uuid.uuid4()
    await run_replay(
        [CaseSpec(did)],
        load=_loader({did: repo.input(recorded_base=repo.a, created_at=None)}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )
    assert calls == [repo.a]


async def test_unknown_base_or_unappliable_diff_never_calls_stage_a(
    repo: _Repo, tmp_path: Path
) -> None:
    async def stage_a(**_: Any) -> dict[str, Any]:  # pragma: no cover — must not run
        raise AssertionError("Stage A must not be called")

    bogus = repo.diff.replace("index ", "index ffffff", 1)  # a blob no commit has
    ids = [uuid.uuid4() for _ in range(5)]
    inputs = {
        ids[0]: repo.input(diff=bogus),
        # recorded base is C: the diff (made on A) does not apply there
        ids[1]: repo.input(recorded_base=repo.c),
        ids[2]: repo.input(recorded_base="deadbeef" * 5),
        ids[3]: repo.input(diff_truncated=True),
        # created before A existed: no candidate commit
        ids[4]: repo.input(created_at=datetime(2025, 1, 1, tzinfo=UTC)),
    }
    out = await run_replay(
        [CaseSpec(i) for i in ids],
        load=_loader(inputs),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )

    assert [r["status"] for r in out["results"]] == ["skipped"] * 5
    assert all(r["reason"] and r["runs"] == [] for r in out["results"])
    assert "적용되지 않는다" in out["results"][1]["reason"]
    assert list((tmp_path / "work").glob("replay-*")) == []  # nothing left behind


async def test_repeat_three_runs_stage_a_three_times_on_independent_trees(
    repo: _Repo, tmp_path: Path
) -> None:
    roots: list[Path] = []

    async def stage_a(**kw: Any) -> dict[str, Any]:
        root = Path(kw["box"].workspace_mount)
        roots.append(root)
        assert not (root / "scribble.txt").exists()  # nothing a previous round left
        (root / "scribble.txt").write_text("left by this round")
        return _blob()

    did = uuid.uuid4()
    out = await run_replay(
        [CaseSpec(did)],
        repeat=3,
        load=_loader({did: repo.input()}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )

    assert len(roots) == 3 and len(set(roots)) == 3
    assert out["results"][0]["repeat"] == 3 and len(out["results"][0]["runs"]) == 3
    assert list((tmp_path / "work").glob("replay-*")) == []


async def test_replay_writes_nothing_and_removes_its_trees(repo: _Repo, tmp_path: Path) -> None:
    runs: list[Any] = []

    async def stage_a(**kw: Any) -> dict[str, Any]:
        runs.append(kw["run"])
        kw["run"].usage_prompt_tokens += 7
        kw["run"].usage_completion_tokens += 3
        return _blob(1)

    did = uuid.uuid4()
    out = await run_replay(
        [CaseSpec(did)],
        load=_loader({did: repo.input(intent_text="do it")}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )

    # a stand-in, not an ExecutionRun: nothing in the replay path has a session to write with
    assert isinstance(runs[0], SimpleNamespace) and runs[0].payload == {"intent_text": "do it"}
    run = out["results"][0]["runs"][0]
    assert run["tokens"] == {"prompt": 7, "completion": 3, "total": 10}
    assert run["seconds"] >= 0
    assert run["confirmed_findings"] == [{"claim": "bug 0", "file": "a.py", "line": 2}]
    assert list((tmp_path / "work").iterdir()) == []


async def test_batch_scoring_rates(repo: _Repo, tmp_path: Path) -> None:
    outcomes = iter([1, 0, 1, 0])  # defect1 confirmed, defect2 not, clean1 confirmed, clean2 not

    async def stage_a(**_: Any) -> dict[str, Any]:
        return _blob(next(outcomes))

    ids = [uuid.uuid4() for _ in range(4)]
    cases = [
        CaseSpec(ids[0], "defect", "note"),
        CaseSpec(ids[1], "defect"),
        CaseSpec(ids[2], "clean"),
        CaseSpec(ids[3], "clean"),
    ]
    out = await run_replay(
        cases,
        load=_loader({i: repo.input() for i in ids}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )

    summary = out["summary"]
    assert summary["defect_confirmed_rate"] == 0.5
    assert summary["false_confirmed_rate"] == 0.5
    assert (summary["defect_cases"], summary["clean_cases"], summary["skipped"]) == (2, 2, 0)
    assert [r["any_confirmed"] for r in out["results"]] == [True, False, True, False]
    assert out["results"][0]["defect_note"] == "note"
    assert out["results"][0]["confirmed_runs"] == 1 and out["results"][0]["repeat"] == 1
    assert out["results"][0]["runs"][0]["confirmed_findings"][0]["claim"] == "bug 0"


async def test_repeat_is_capped(repo: _Repo, tmp_path: Path) -> None:
    did = uuid.uuid4()
    kw: dict[str, Any] = {
        "load": _loader({did: repo.input()}),
        "llm": object(),
        "box_for": _noop_box,
        "work_root": tmp_path / "work",
    }
    with pytest.raises(ReplayRefused):
        await run_replay([CaseSpec(did)], repeat=replay.MAX_REPEAT + 1, **kw)
    with pytest.raises(ReplayRefused):
        await run_replay([CaseSpec(did)] * (replay.MAX_CASES + 1), **kw)
    with pytest.raises(ValueError):  # the MCP schema refuses it too
        AdversarialReplayInput(deliverable_id=did, repeat=replay.MAX_REPEAT + 1)
    assert AdversarialReplayInput(deliverable_id=did).repeat == 1


async def test_only_one_replay_at_a_time(repo: _Repo, tmp_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def stage_a(**_: Any) -> dict[str, Any]:
        started.set()
        await release.wait()
        return _blob()

    did = uuid.uuid4()
    kw: dict[str, Any] = {
        "load": _loader({did: repo.input()}),
        "llm": object(),
        "box_for": _noop_box,
        "work_root": tmp_path / "work",
        "stage_a": stage_a,
    }
    first = asyncio.create_task(run_replay([CaseSpec(did)], **kw))
    await started.wait()
    with pytest.raises(ReplayRefused):
        await run_replay([CaseSpec(did)], **kw)
    release.set()
    assert (await first)["results"][0]["status"] == "ran"
    release.clear()
    started.clear()
    release.set()
    assert (await run_replay([CaseSpec(did)], **kw))["results"][0]["status"] == "ran"  # free again


async def test_replay_runs_through_the_real_executor_adapter(
    repo: _Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production round (``run_adversarial_round``), real ExecutorAdapter, fake CLI only."""
    workspace_id = uuid.uuid4()
    queue = [json.dumps({"tool": "grep", "args": {"pattern": "return"}}), '{"findings": []}']
    prompts: list[Any] = []

    async def _no_token(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the verifier must not be issued a run-scoped work-tool token")

    async def _fake_cli(*_a: Any, **kw: Any) -> _StubCompletedTask:
        prompts.append(kw)
        return _StubCompletedTask(status="done", output=queue.pop(0))

    monkeypatch.setattr(oauth_service, "issue_run_task_token", _no_token)
    monkeypatch.setattr(dispatch, "await_completion", _fake_cli)

    did = uuid.uuid4()
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
                run_id=None,
            )
            out = await run_replay(
                [CaseSpec(did)],
                load=_loader({did: repo.input()}),
                llm=ResolverLoopLlm(adapter=adapter),
                box_for=_noop_box,
                work_root=tmp_path / "work",
            )

    run = out["results"][0]["runs"][0]
    assert run["stage_a"]["status"] == "done", run["stage_a"]
    assert run["stage_a"]["probes"] >= 1
    assert len(prompts) == 2  # tool request → harness output → final answer
    assert run["base_commit"] == repo.a
    assert _git(repo.path, "status", "--porcelain") == ""
