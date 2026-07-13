"""GitHub delivery end-to-end — verified Deliverable → a github Pull Request.

github is the one delivery target that needs a real DIFF: the run must WORK
INSIDE a clone of the target repo, then commit + push the branch + open a PR.
This proves that whole loop WITHOUT touching github.com:

* a LOCAL bare git repo (``git init --bare``) stands in for the "remote",
* the run-setup provisioner clones THAT bare repo into the run workspace on a
  ``bsvibe/run-<id>`` branch (``remote_url_for`` points at the local bare repo),
* the scripted work-LLM writes a file into the checkout → verified,
* the delivery handler ``commit_all`` + ``push`` the branch to the bare remote
  (asserted: the branch + commit landed) and calls the github ``open_pr`` action
  (respx-mocks the github REST API) → a ``DeliveryResult`` carries the PR ref.

Plus the no-change branch: a run that writes nothing → no push, no PR, clean
success (no empty PR is opened).

In-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL`` is set
(mirrors the other glue tests).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.loader import PluginLoader
from backend.extensions.skill.loader import SkillLoader
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from backend.workflow.application.delivery.connector_dispatch import (
    build_connector_delivery_adapter,
    build_github_workspace_provisioner,
)
from backend.workflow.application.delivery.connector_dispatch._github import (
    _source_github_issue_number,
)
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps, AgentWorker
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)
from plugin.github import plugin as github_module

from .._support import BuildFrameLlm, db_engine

GITHUB_API = "https://api.github.test"
TEST_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


# --------------------------------------------------------------------------
# git helpers — a local bare repo as the "remote"
# --------------------------------------------------------------------------


async def _git(*args: str, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def _make_bare_remote(tmp_path: Path) -> Path:
    """A bare repo seeded with an initial commit on ``main``."""
    bare = tmp_path / "remote.git"
    await _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    await _git("clone", str(bare), str(seed))
    await _git("config", "user.email", "t@bsvibe.dev", cwd=seed)
    await _git("config", "user.name", "Test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    await _git("add", "-A", cwd=seed)
    await _git("commit", "-m", "initial", cwd=seed)
    await _git("push", "origin", "main", cwd=seed)
    return bare


# --------------------------------------------------------------------------
# Test doubles + seeding
# --------------------------------------------------------------------------


class _ScriptedLlm:
    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted")
        return self._turns.pop(0)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _scripted_writes_file() -> _ScriptedLlm:
    """Declare a command check + write a NEW file into the checkout → verified."""
    return _ScriptedLlm(
        [
            LoopTurn(
                content="Adding the feature file and declaring how to check it.",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "test -f feature.txt"}],
                    ),
                    _tc("file_write", path="feature.txt", content="the feature\n"),
                ),
            ),
            LoopTurn(content="Add the feature\n\nImplements the requested feature.", tool_calls=()),
        ]
    )


def _scripted_no_file_changes() -> _ScriptedLlm:
    """Declare a check that passes WITHOUT writing any file (README already exists
    in the clone) → verified but the checkout has no changes."""
    return _ScriptedLlm(
        [
            LoopTurn(
                content="Verifying the existing README is present.",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[{"kind": "command", "command": "test -f README.md"}],
                    ),
                ),
            ),
            LoopTurn(content="Nothing to change — README already present.", tool_calls=()),
        ]
    )


async def _seed_github_connector(
    session: AsyncSession, cipher: CredentialCipher, workspace_id: uuid.UUID
) -> None:
    session.add(
        ConnectorAccountRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("ghp_test_token"),
            delivery_config={
                "repo": "owner/name",
                "base_branch": "main",
                "github_api_url": GITHUB_API,
            },
            is_active=True,
        )
    )
    await session.commit()


async def _seed_open_run(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    """An OPEN ExecutionRun ready to be driven.

    ``request_id`` is left None so framing (which would need a TriggerEvent FK)
    is skipped — the github provisioner + the loop only need the run's
    ``workspace_id`` + ``id``, both set here. The intent text is seeded directly
    into the run payload (what FrameStage would otherwise fold in)."""
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        request_id=None,
        status=RunStatus.OPEN,
        payload={"intent_text": "add the feature"},
    )
    session.add(run)
    await session.commit()
    return run.id


def _execution_deps(
    sf_: async_sessionmaker[AsyncSession],
    workspace_root: Path,
    cipher: CredentialCipher,
    bare: Path,
    llm: _ScriptedLlm,
) -> AgentExecutionDeps:
    def _skill_loader_for(ws_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(workspace_root / "skills" / str(ws_id))
        loader.load_all()
        return loader

    # The run-setup provisioner clones the LOCAL bare repo (not github.com).
    provisioner = build_github_workspace_provisioner(
        cipher=cipher, remote_url_for=lambda _repo: bare.as_uri()
    )
    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=lambda session, _run: RunOrchestrator(
            session=session, llm=llm, sandbox_manager=NoopSandboxManager()
        ),
        workspace_root=workspace_root,
        workspace_provisioner=provisioner,
        # prod always routes a frame model; the stage refuses to guess without one
        frame_llm=BuildFrameLlm(),
    )


async def _plugins():
    impl_dir = Path(github_module.__file__).resolve().parents[1]
    return await PluginLoader(impl_dir).load_all()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@respx.mock
async def test_verified_run_delivers_as_github_pr(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    workspace_id = uuid.uuid4()
    bare = await _make_bare_remote(tmp_path)
    workspace_root = tmp_path / "runs"

    pr_route = respx.post(f"{GITHUB_API}/repos/owner/name/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": 7, "html_url": "https://github.com/owner/name/pull/7"}
        )
    )

    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)
        run_id = await _seed_open_run(s, workspace_id)

    # 1. Drive the run — the provisioner clones the bare repo onto a per-run
    #    branch, the work-LLM writes feature.txt into THAT checkout → verified.
    deps = _execution_deps(sf, workspace_root, cipher, bare, _scripted_writes_file())
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    branch = f"bsvibe/run-{run_id.hex[:8]}"
    checkout = workspace_root / str(run_id)
    assert (checkout / "feature.txt").read_text() == "the feature\n"
    # The clone is a real checkout of the bare remote on the per-run branch.
    assert (await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout)) == branch

    async with sf() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.REVIEW_READY
        deliverable = (await s.execute(select(Deliverable))).scalar_one()
        deliverable_id = deliverable.id

    # 2. Delivery: commit + push the branch + open the PR.
    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf,
        plugins=list(registry.values()),
        cipher=cipher,
        workspace_root=workspace_root,
        remote_url_for=lambda _repo: bare.as_uri(),
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    # The branch + commit landed on the bare remote.
    branches = await _git("branch", "--list", cwd=bare)
    assert branch in branches
    files = await _git("ls-tree", "--name-only", branch, cwd=bare)
    assert "feature.txt" in files
    log = await _git("log", branch, "--oneline", cwd=bare)
    # Commit message = first line of summary = the founder intent (the summary is
    # now titled by intent_text, not the work LLM's free output).
    assert "add the feature" in log.lower()

    # open_pr was requested with head=run branch, base=main, title/body from
    # the deliverable summary.
    assert pr_route.called
    body = pr_route.calls.last.request.content.decode()
    assert f'"head": "{branch}"' in body or f'"head":"{branch}"' in body
    assert '"base": "main"' in body or '"base":"main"' in body
    # PR title/body = intent title + the deterministic changed-file list (F4),
    # NOT the work LLM's raw narration.
    assert "add the feature" in body.lower()
    assert "feature.txt" in body
    # Token came from the decrypted connector secret.
    assert pr_route.calls.last.request.headers["authorization"] == "Bearer ghp_test_token"

    # Direct delivery result is recorded against the deliverable + the event drained.
    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None
        assert await s.get(Deliverable, deliverable_id) is not None


@respx.mock
async def test_github_no_file_changes_no_push_no_pr_clean_success(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    """A verified run in a github workspace that edits NO files → no push, no
    PR, clean no-op success (no empty PR is opened)."""
    workspace_id = uuid.uuid4()
    bare = await _make_bare_remote(tmp_path)
    workspace_root = tmp_path / "runs"

    pr_route = respx.post(f"{GITHUB_API}/repos/owner/name/pulls")

    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)
        run_id = await _seed_open_run(s, workspace_id)

    deps = _execution_deps(sf, workspace_root, cipher, bare, _scripted_no_file_changes())
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    branch = f"bsvibe/run-{run_id.hex[:8]}"

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf,
        plugins=list(registry.values()),
        cipher=cipher,
        workspace_root=workspace_root,
        remote_url_for=lambda _repo: bare.as_uri(),
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    # No PR requested, branch never pushed (only the seeded main exists).
    assert not pr_route.called
    branches = await _git("branch", "--list", cwd=bare)
    assert branch not in branches

    async with sf() as s:
        assert (await s.execute(select(DeliveryEventRow))).first() is None


# --------------------------------------------------------------------------
# Closing the loop back to the originating github issue (Closes #N + comment)
# --------------------------------------------------------------------------


async def _seed_run_from_github_issue(
    session: AsyncSession, workspace_id: uuid.UUID, issue_number: int
) -> uuid.UUID:
    """An OPEN run whose Request traces back to a github ISSUE TriggerEvent — so
    the delivery can close the loop (``Closes #N`` ref + a PR-link comment). The
    issue number lives ONLY on the trigger envelope (the run payload keeps just
    ``intent_text``), mirroring the real intake path."""
    trigger = TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        idempotency_key=uuid.uuid4().hex,
        payload={
            "github_event": "issues",
            "action": "opened",
            "repo": "owner/name",
            "intent_text": "add the feature",
            "body": {"issue": {"number": issue_number, "title": "Add the feature"}},
        },
    )
    session.add(trigger)
    await session.flush()
    request = RequestRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trigger_event_id=trigger.id,
        payload={},
    )
    session.add(request)
    await session.flush()
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        request_id=request.id,
        status=RunStatus.OPEN,
        payload={"intent_text": "add the feature"},
    )
    session.add(run)
    await session.commit()
    return run.id


@respx.mock
async def test_github_pr_closes_and_comments_on_source_issue(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, tmp_path: Path
) -> None:
    """A run triggered by a github ISSUE → the delivered PR references the issue
    (``Closes #42``, which cross-links it and auto-closes on merge) AND a comment
    with the PR link lands on the issue — the loop is closed back to the filer."""
    workspace_id = uuid.uuid4()
    bare = await _make_bare_remote(tmp_path)
    workspace_root = tmp_path / "runs"

    pr_route = respx.post(f"{GITHUB_API}/repos/owner/name/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": 7, "html_url": "https://github.com/owner/name/pull/7"}
        )
    )
    comment_route = respx.post(f"{GITHUB_API}/repos/owner/name/issues/42/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 555,
                "html_url": "https://github.com/owner/name/issues/42#issuecomment-555",
            },
        )
    )

    async with sf() as s:
        await _seed_github_connector(s, cipher, workspace_id)
        await _seed_run_from_github_issue(s, workspace_id, 42)

    deps = _execution_deps(sf, workspace_root, cipher, bare, _scripted_writes_file())
    agent = AgentWorker(session_factory=sf, execution=deps)
    assert await agent.drive_once() == 1

    registry = await _plugins()
    adapter = build_connector_delivery_adapter(
        session_factory=sf,
        plugins=list(registry.values()),
        cipher=cipher,
        workspace_root=workspace_root,
        remote_url_for=lambda _repo: bare.as_uri(),
    )
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=adapter,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1

    # The PR body references the originating issue.
    assert pr_route.called
    pr_body = pr_route.calls.last.request.content.decode()
    assert "Closes #42" in pr_body

    # A comment carrying the PR link landed on the source issue.
    assert comment_route.called
    comment_body = comment_route.calls.last.request.content.decode()
    assert "pull/7" in comment_body


async def test_source_issue_number_none_for_direct_run(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A Direct chat run (no Request/TriggerEvent) has no issue to reply to — the
    tracer returns None so non-github delivery is untouched (no Closes/comment)."""
    workspace_id = uuid.uuid4()
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            request_id=None,
            status=RunStatus.OPEN,
            payload={"intent_text": "just chatting"},
        )
        s.add(run)
        await s.commit()
        assert await _source_github_issue_number(s, run.id) is None
        assert await _source_github_issue_number(s, None) is None
