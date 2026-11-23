"""The base-tip fetch runs before the push on the REAL delivery path, and on "send again".

``fetch_base_tip`` is NOT faked: a real ``GitOps`` over a local bare remote records the order
of its calls, so the test fails if ``deliver_github`` stops fetching before it pushes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.data.rls import workspace_session_scope
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.delivery.connector_dispatch import ConnectorDeliveryAdapter
from backend.workflow.application.delivery.connector_dispatch._github import (
    GithubDeliveryDeps,
    deliver_github,
    run_branch_name,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import GithubBinding
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.delivery.git_ops import GitOps
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


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


class _RecordingGitOps(GitOps):
    """The real ``GitOps``; only records the order its delivery-relevant methods are called."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def fetch_base_tip(self, dest: Path, base_branch: str, *, token: str | None) -> bool:
        self.calls.append("fetch_base_tip")
        return await super().fetch_base_tip(dest, base_branch, token=token)

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        self.calls.append("push")
        await super().push(dest, branch, token=token)


class _Cipher:
    def encrypt(self, plaintext: str) -> str:
        return f"ct:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("ct:")


class _Runner:
    async def dispatch_action(self, plugin, *, action_name, context, kwargs):  # type: ignore[no-untyped-def]
        return {"url": "https://github.com/owner/name/pull/1"}


async def _remote_and_checkout(tmp_path: Path, run_id: uuid.UUID) -> tuple[Path, Path]:
    """A bare remote + a shallow run checkout at ``<root>/<run_id>`` with a new commit on its
    run branch; origin's ``main`` then moves on (the 408 scenario)."""
    bare = tmp_path / "remote.git"
    await _git("init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    await _git("clone", str(bare), str(seed))
    await _git("config", "user.email", "t@bsvibe.dev", cwd=seed)
    await _git("config", "user.name", "T", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    await _git("add", "-A", cwd=seed)
    await _git("commit", "-m", "initial", cwd=seed)
    await _git("push", "origin", "main", cwd=seed)

    root = tmp_path / "ws"
    root.mkdir()
    checkout = root / str(run_id)
    ops = GitOps()
    await ops.clone(bare.as_uri(), checkout, token=None, depth=1)
    await ops.checkout_new_branch(checkout, run_branch_name(run_id))
    (checkout / "answer.txt").write_text("42\n")

    (seed / "later.txt").write_text("main moved\n")
    await _git("add", "-A", cwd=seed)
    await _git("commit", "-m", "main moved", cwd=seed)
    await _git("push", "origin", "main", cwd=seed)
    return bare, root


def _account(ws: uuid.UUID) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=ws,
        connector="github",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext=_Cipher().encrypt("ghp_secret"),
        delivery_config={"repo": "owner/name"},
        is_active=True,
    )


async def test_deliver_github_fetches_base_tip_before_push(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    bare, root = await _remote_and_checkout(tmp_path, run_id)
    git_ops = _RecordingGitOps()
    ws = uuid.uuid4()
    async with shared_file_sessionmaker() as sf:
        async with sf() as s:
            account = _account(ws)
            s.add(account)
            await s.commit()
            s.expunge(account)
        deps = GithubDeliveryDeps(
            cipher=_Cipher(),  # type: ignore[arg-type]
            plugins_by_name={"github": object()},  # type: ignore[dict-item]
            workspace_root=root,
            git_ops=git_ops,
            remote_url_for=lambda repo: bare.as_uri(),
            runner=_Runner(),  # type: ignore[arg-type]
            session_factory=sf,
        )
        actions = await deliver_github(
            deps=deps,
            binding=GithubBinding(account=account, repo="owner/name", base_branch="main"),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "Title\n\nBody"},
            artifact_type="code",
        )
    assert actions[0].succeeded is True
    assert git_ops.calls == ["fetch_base_tip", "push"]
    assert run_branch_name(run_id) in await _git("branch", "--list", cwd=bare)


async def test_retry_delivery_event_reaches_deliver_github_with_fetch_before_push(
    tmp_path: Path,
) -> None:
    """ "다시 보내기" (``delivery_failed`` → retry) emits the delivery event the worker hands to
    the adapter, whose github path is ``deliver_github`` -- the same fetch→push path."""
    run_id = uuid.uuid4()
    bare, root = await _remote_and_checkout(tmp_path, run_id)
    git_ops = _RecordingGitOps()
    ws = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with shared_file_sessionmaker() as sf:
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(_account(ws))
            s.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    product_id=None,
                    status=RunStatus.REVIEW_READY,
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.flush()
            s.add(
                Deliverable(
                    id=deliverable_id,
                    run_id=run_id,
                    workspace_id=ws,
                    deliverable_type=DeliverableType.PR,
                    payload={"summary": "Title\n\nBody"},
                    created_at=now,
                )
            )
            decision = Decision(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=ws,
                decision="delivery_failed",
                payload={"deliverable_id": str(deliverable_id)},
                status=DecisionStatus.PENDING,
            )
            s.add(decision)
            await s.commit()
            decision_id = decision.id

        async with sf() as s, workspace_session_scope(s, ws):
            await resolve_checkpoint(
                s,
                workspace_id=ws,
                checkpoint_id=decision_id,
                action_key="retry",
                actor_id=uuid.uuid4(),
            )
            await s.commit()

        async with sf() as s, workspace_session_scope(s, ws):
            from sqlalchemy import select  # noqa: PLC0415

            event = (
                await s.execute(
                    select(DeliveryEventRow).where(
                        DeliveryEventRow.deliverable_id == deliverable_id
                    )
                )
            ).scalar_one()

        adapter = ConnectorDeliveryAdapter(
            session_factory=sf,
            plugins_by_name={"github": SimpleNamespace(outbounds=[])},  # type: ignore[dict-item]
            cipher=_Cipher(),  # type: ignore[arg-type]
            workspace_root=root,
            git_ops=git_ops,
            remote_url_for=lambda repo: bare.as_uri(),
            runner=_Runner(),  # type: ignore[arg-type]
        )
        result = await adapter.dispatch(
            workspace_id=event.workspace_id,
            deliverable_id=event.deliverable_id,
            artifact_type=event.artifact_type,
        )

    assert any(a.succeeded for a in result.actions), result.actions
    assert git_ops.calls == ["fetch_base_tip", "push"]
