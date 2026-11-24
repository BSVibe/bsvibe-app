"""Stage A replay over a past deliverable (``backend.workflow.application.adversarial_replay``).

Real git repositories (a base commit A, a later commit C on ``main``, and a diff made on top of
A); only the judge model and — in the scoring tests — Stage A itself are faked. One test drives a
whole replay through the real ExecutorAdapter path like ``test_adversarial_executor_path.py``.
"""

from __future__ import annotations

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
from backend.mcp.tools.adversarial_replay_tools import (
    AdversarialReplayInput,
    AdversarialReplayStatusInput,
    _h_adversarial_replay,
    _h_adversarial_replay_status,
)
from backend.workflow.application import adversarial_replay as replay
from backend.workflow.application import adversarial_replay_jobs as jobs
from backend.workflow.application.adversarial_replay import (
    CaseSpec,
    ReplayInput,
    ReplayRefused,
    run_replay,
)
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime import account_resolution
from backend.workflow.application.runtime.adversarial_replay_runtime import judge_llm_for
from backend.workflow.infrastructure.db import (
    AdversarialReplayJob,
    Deliverable,
    ExecutionRun,
    UnattributedLlmUsage,
)
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession
from backend.workflow.infrastructure.workers.adversarial_replay_worker import (
    AdversarialReplayWorker,
)

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


# ---------------------------------------------------------------------------
# the background job (the MCP request only registers; the worker runs; status reads)
# ---------------------------------------------------------------------------
def _specs(n: int = 1) -> list[CaseSpec]:
    return [CaseSpec(uuid.uuid4(), "defect") for _ in range(n)]


async def _count(sf: Any, model: Any) -> int:
    from sqlalchemy import func, select  # noqa: PLC0415

    async with sf() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_only_one_job_runs_at_a_time_by_db_state() -> None:
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as sf:
        async with sf() as session:
            first = await jobs.submit_job(session, ws, _specs(), repeat=1, batch=False)
            with pytest.raises(ReplayRefused):  # a second while one is queued
                await jobs.submit_job(session, ws, _specs(), repeat=1, batch=False)
            other = await jobs.submit_job(session, uuid.uuid4(), _specs(), repeat=1, batch=False)

        claimed = await jobs.claim_next(sf)
        assert claimed is not None and claimed.id == first.id  # oldest first
        # `first` is running with a live heartbeat: nothing else is handed out, from ANY worker
        assert await jobs.claim_next(sf) is None
        async with sf() as session:
            with pytest.raises(ReplayRefused):
                await jobs.submit_job(session, ws, _specs(), repeat=1, batch=False)

        # the worker died: its heartbeat lapses, so the slot is freed and the next job is claimed
        from datetime import timedelta  # noqa: PLC0415

        async with sf() as session:
            row = await session.get(AdversarialReplayJob, first.id)
            assert row is not None
            row.heartbeat_at = datetime.now(UTC) - timedelta(seconds=jobs.LEASE_S + 5)
            await session.commit()
        nxt = await jobs.claim_next(sf)
        assert nxt is not None and nxt.id == other.id
        async with sf() as session:
            dead = await session.get(AdversarialReplayJob, first.id)
            assert dead is not None and dead.status == jobs.FAILED and dead.error


async def test_a_case_exception_fails_only_that_case(repo: _Repo, tmp_path: Path) -> None:
    calls = {"n": 0}

    async def stage_a(**_: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("judge exploded")
        return _blob(1)

    ids = [uuid.uuid4(), uuid.uuid4()]
    out = await run_replay(
        [CaseSpec(ids[0], "defect"), CaseSpec(ids[1], "defect")],
        load=_loader({i: repo.input() for i in ids}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )
    first, second = out["results"]
    assert first["status"] == "failed" and "judge exploded" in first["reason"]
    assert second["status"] == "ran" and second["any_confirmed"] is True
    assert out["summary"]["failed"] == 1
    assert list((tmp_path / "work").glob("replay-*")) == []


async def test_job_worker_runs_and_status_reports_scoring(repo: _Repo, tmp_path: Path) -> None:
    ws = uuid.uuid4()
    outcomes = iter([1, 0, 1, 0])  # defect: 1/2 confirmed, clean: 1/2 confirmed

    async def stage_a(**_: Any) -> dict[str, Any]:
        return _blob(next(outcomes))

    ids = [uuid.uuid4() for _ in range(4)]
    cases = [
        CaseSpec(ids[0], "defect", "note"),
        CaseSpec(ids[1], "defect"),
        CaseSpec(ids[2], "clean"),
        CaseSpec(ids[3], "clean"),
    ]
    inputs = {i: repo.input() for i in ids}

    async with shared_file_sessionmaker() as sf:
        async with sf() as session:
            registered = await jobs.submit_job(session, ws, cases, repeat=1, batch=True)

        async def execute(job: AdversarialReplayJob) -> None:
            async def llm_for(_ws: uuid.UUID) -> Any:
                return object()

            await jobs.execute_job(
                sf,
                job,
                llm_for=llm_for,
                load_for=lambda _ws: _loader(inputs),
                box_for=_noop_box,
                work_root=tmp_path / "work",
                stage_a=stage_a,
            )

        worker = AdversarialReplayWorker(session_factory=sf, execute=execute)
        assert await worker.run_once() == 1
        assert await worker.run_once() == 0  # nothing left

        # a fresh session (as after a restart) reads the stored state
        async with sf() as session:
            status = await jobs.job_status(session, ws, registered.id)
            assert await jobs.job_status(session, uuid.uuid4(), registered.id) is None
    assert status is not None and status["status"] == "done"
    assert status["progress"] == {"cases_total": 4, "cases_done": 4}
    assert status["summary"]["defect_confirmed_rate"] == 0.5
    assert status["summary"]["false_confirmed_rate"] == 0.5
    assert status["results"][0]["defect_note"] == "note"
    assert status["results"][0]["runs"][0]["confirmed_findings"][0]["claim"] == "bug 0"


async def test_mcp_tools_register_at_once_and_read_status() -> None:
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as sf:
        async with sf() as session:
            ctx = SimpleNamespace(session=session, principal=SimpleNamespace(workspace_id=ws))
            did = uuid.uuid4()
            out = (
                await _h_adversarial_replay(
                    AdversarialReplayInput(deliverable_id=did, repeat=3), ctx
                )  # type: ignore[arg-type]
            ).root
            assert out["status"] == "queued" and out["repeat"] == 3
            status = (
                await _h_adversarial_replay_status(
                    AdversarialReplayStatusInput(replay_id=uuid.UUID(out["replay_id"])),
                    ctx,  # type: ignore[arg-type]
                )
            ).root
            assert status["status"] == "queued" and status["results"] == []
            from backend.mcp.api import ToolError  # noqa: PLC0415

            with pytest.raises(ToolError):  # one at a time
                await _h_adversarial_replay(AdversarialReplayInput(deliverable_id=did), ctx)  # type: ignore[arg-type]
            with pytest.raises(ToolError):
                await _h_adversarial_replay_status(
                    AdversarialReplayStatusInput(replay_id=uuid.uuid4()),
                    ctx,  # type: ignore[arg-type]
                )


async def test_fake_run_id_records_no_usage_or_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge path is handed a stand-in run; none of its id may reach the adapter."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*_a: Any, **kw: Any) -> None:
        seen.update(kw)
        return None

    monkeypatch.setattr(account_resolution, "resolve_via_caller", fake_resolve)
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as sf:
        assert await judge_llm_for(sf, get_settings(), ws) is None
        assert seen["workspace_id"] == ws
        assert seen.get("run_id") is None  # not bound to a run: no meter, no run link
        for model in (ExecutionRun, Deliverable, UnattributedLlmUsage):
            assert await _count(sf, model) == 0


# ---------------------------------------------------------------------------
# base commit search over main AND origin/main, with a fetch first
# ---------------------------------------------------------------------------
def _origin_only_base(repo: _Repo) -> tuple[str, str]:
    """Base X lives only on a GitHub-side branch off A; local main is diverged (C)."""
    _git(repo.path, "checkout", "-q", "--detach", repo.a)
    (repo.path / "a.py").write_text("def f():\n    return 1  # github\n")
    _git(repo.path, "commit", "-qam", "gh", date="2026-01-15T00:00:00Z")
    x = _git(repo.path, "rev-parse", "HEAD")
    (repo.path / "a.py").write_text("def f():\n    return 41\n")
    (repo.path / "new.py").write_text("X = 1\n")
    _git(repo.path, "add", "-A")
    diff = _git(repo.path, "diff", "--cached", "--binary") + "\n"
    _git(repo.path, "reset", "-q", "--hard", x)
    _git(repo.path, "clean", "-fdq")
    _git(repo.path, "checkout", "-q", "main")
    return x, diff


async def test_base_commit_only_on_origin_main_is_found(repo: _Repo) -> None:
    x, diff = _origin_only_base(repo)
    _git(repo.path, "update-ref", "refs/remotes/origin/main", x)
    assert await replay.find_base_commit(repo.path, repo.input(diff=diff)) == x


def _bundle_restored_product(repo: _Repo, root: Path, github: Path) -> Path:
    """A product repo restored the way ``ensure_product_workspace`` does: cloned from a bundle
    that is then deleted, so ``origin`` is a dead temp path. ``github`` holds the base commit."""
    bundle = root / "restore.bundle"
    _git(repo.path, "bundle", "create", str(bundle), "main")
    product = root / "restored"
    _git(root, "clone", "-q", "--branch", "main", str(bundle), str(product))
    bundle.unlink()
    github.mkdir()
    _git(github, "init", "-q", "--bare", "-b", "main")
    return product


def _loader_env(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch, product: Path, diff: str, repo_url: str | None
) -> tuple[Any, uuid.UUID, uuid.UUID, dict[str, Any]]:
    """Only the DB rows, the restore step and the credential are stubbed; the fetch is real."""
    ws, pid, did = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen: dict[str, Any] = {}
    deliverable = SimpleNamespace(
        workspace_id=ws,
        run_id=uuid.uuid4(),
        payload={"diff": diff},
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    run = SimpleNamespace(product_id=pid, payload={})
    product_row = SimpleNamespace(repo_url=repo_url)

    class _Session:
        async def get(self, model: Any, _id: Any) -> Any:
            return {"Deliverable": deliverable, "ProductRow": product_row}.get(model.__name__, run)

    async def ensure(_pid: uuid.UUID) -> bool:
        return True

    async def token(_session: Any, *, workspace_id: uuid.UUID, cipher: Any) -> str:
        assert workspace_id == ws
        seen["token"] = "ghs_workspace_token"
        return "ghs_workspace_token"

    monkeypatch.setattr("backend.storage.product_workspace.ensure_product_workspace", ensure)
    monkeypatch.setattr(
        "backend.storage.product_workspace.product_workspace_path", lambda _p: product
    )
    monkeypatch.setattr("backend.connectors.auth.resolve.resolve_github_token", token)
    monkeypatch.setattr("backend.router.accounts.crypto.cipher_from_settings", lambda: object())
    return _Session(), ws, did, seen


async def test_bundle_restored_repo_finds_base_via_product_repo_url(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    x, diff = _origin_only_base(repo)
    github = tmp_path / "github"
    product = _bundle_restored_product(repo, tmp_path, github)
    _git(repo.path, "push", "-q", str(github), f"{x}:refs/heads/main")
    origin_before = _git(product, "remote", "get-url", "origin")
    main_before = _git(product, "rev-parse", "main")
    assert _git(product, "cat-file", "-t", main_before) == "commit"
    session, ws, did, seen = _loader_env(repo, monkeypatch, product, diff, str(github))

    inp = await replay.load_replay_input(session, ws, did)

    assert seen["token"] == "ghs_workspace_token"
    assert inp.fetch_failed is False
    assert await replay.find_base_commit(product, inp) == x
    assert _git(product, "remote", "get-url", "origin") == origin_before
    assert _git(product, "rev-parse", "main") == main_before
    assert "ghs_workspace_token" not in (product / ".git" / "config").read_text()


@pytest.mark.parametrize("kind", ["no_url", "unreachable"])
async def test_missing_or_unreachable_repo_url_reports_fetch_failed(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    x, diff = _origin_only_base(repo)
    product = _bundle_restored_product(repo, tmp_path, tmp_path / "github")
    url = None if kind == "no_url" else str(tmp_path / "nowhere")
    session, ws, did, _ = _loader_env(repo, monkeypatch, product, diff, url)
    main_before = _git(product, "rev-parse", "main")

    inp = await replay.load_replay_input(session, ws, did)

    assert inp.fetch_failed is True
    with pytest.raises(replay.CaseUnavailable, match="받아 오지 못했다"):
        await replay.find_base_commit(product, inp)
    assert _git(product, "rev-parse", "main") == main_before


async def test_replay_tree_is_self_contained_without_product_repo(
    repo: _Repo, tmp_path: Path
) -> None:
    """Only the copy's directory is mounted in the sandbox: no alternates back to the product."""
    dest = tmp_path / "copy"
    base = await replay.reconstruct_tree(repo.input(), dest)
    before = (_git(repo.path, "rev-parse", "HEAD"), _git(repo.path, "count-objects", "-v"))

    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    moved = tmp_path / "moved-away"
    repo.path.rename(moved)  # the product repo is no longer reachable at its old path
    assert _git(dest, "rev-parse", "HEAD") == base == repo.a
    assert "new.py" in _git(dest, "status", "--porcelain")
    assert "X = 1" in _git(dest, "diff", "HEAD") or (dest / "new.py").read_text() == "X = 1\n"
    assert (dest / "a.py").read_text() == repo.applied_a_py
    _git(dest, "fsck", "--no-dangling")
    moved.rename(repo.path)
    assert (_git(repo.path, "rev-parse", "HEAD"), _git(repo.path, "count-objects", "-v")) == before


async def test_unavailable_stage_a_is_not_scored(repo: _Repo, tmp_path: Path) -> None:
    calls = iter(range(100))

    async def stage_a(**_: Any) -> dict[str, Any]:
        if next(calls) < 2:  # case one: both repeats unavailable
            return {"status": "unavailable", "reason": "boom", "findings": [], "confirmed": 0}
        return _blob(1)

    ids = [uuid.uuid4(), uuid.uuid4()]
    out = await run_replay(
        [CaseSpec(ids[0], "defect"), CaseSpec(ids[1], "defect")],
        repeat=2,
        load=_loader({i: repo.input() for i in ids}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=stage_a,
    )
    first, second = out["results"]
    assert first["status"] == "failed" and "boom" in first["reason"]
    assert second["status"] == "ran"
    s = out["summary"]
    assert (s["failed"], s["defect_cases"], s["defect_confirmed_rate"]) == (1, 1, 1.0)

    async def always_unavailable(**_: Any) -> dict[str, Any]:
        return {"status": "unavailable", "reason": "x", "findings": [], "confirmed": 0}

    out = await run_replay(
        [CaseSpec(ids[0], "defect")],
        load=_loader({ids[0]: repo.input()}),
        llm=object(),
        box_for=_noop_box,
        work_root=tmp_path / "work",
        stage_a=always_unavailable,
    )
    assert out["summary"]["defect_confirmed_rate"] is None
    assert out["summary"]["mean_tokens"] is None


async def test_unavailable_reason_carries_stderr_tail() -> None:
    from backend.workflow.application.adversarial_verifier import run_adversarial_round

    class _Box:
        workspace_mount = "/work"

        async def exec(self, command: str, **_: Any) -> Any:
            return SimpleNamespace(
                exit_code=128,
                stdout="",
                stderr="noise " * 100 + "fatal: not a git repository",
                timed_out=False,
            )

    run = SimpleNamespace(
        id=uuid.uuid4(), payload={}, usage_prompt_tokens=0, usage_completion_tokens=0
    )

    async def release() -> None:
        return None

    blob = await run_adversarial_round(
        llm=object(), run=run, box=_Box(), baseline="x", surface="code", release=release
    )
    assert blob["status"] == "unavailable"
    assert "fatal: not a git repository" in blob["reason"]
