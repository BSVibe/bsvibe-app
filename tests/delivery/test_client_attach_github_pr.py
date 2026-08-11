"""A client_attach run's pushed branch becomes a Pull Request.

#723 turned github delivery off for this execution model at the RESOLVER — no
binding, therefore no PR, therefore nothing. The reason given was true ("the
server never clones this product, so there is no checkout to commit and push
from") and the conclusion was too wide: it also removed the parts that need no
checkout at all. Opening a PR is an API call about a branch that already exists
on github.

Since #735 the branch DOES exist: the run commits its own work in its worktree
on the founder's machine and pushes it with the founder's own credential. So
delivery's job here is the half the server can still legitimately do — resolve
the credential, open the PR, write its URL back onto the Deliverable, close the
loop to the originating issue.

Two things must stay impossible, and both are asserted below:

* **The server must never obtain the source.** Not a clone, not a commit, not a
  push — §3.5 privacy contract. The git ops double here RAISES if touched.
* **No empty PR.** The server-side model's rule is "no changes → no PR"; its
  equivalent here is asking github whether the branch is ahead of base, because
  a local flag can disagree with what actually landed on the remote.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.workflow.application.delivery.connector_dispatch._github import (
    GithubDeliveryDeps,
    deliver_github,
)
from backend.workflow.application.delivery.connector_dispatch._resolver import GithubBinding
from backend.workflow.domain.client_worktree import worktree_branch
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio

_TOKEN = "ghu_oauth_access"  # noqa: S105 — test fixture


class _RoundtripCipher:
    def encrypt(self, plaintext: str) -> str:
        return f"ct:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("ct:")


class _ForbiddenGitOps:
    """Every server-side git operation on a client_attach product is a bug.

    Not a spy that records — a trap that raises. Recording would let the
    assertion be forgotten; raising makes any future caller fail here.
    """

    async def clone(self, repo_url: str, dest: Path, *, token: str | None, depth: int = 1) -> None:
        raise AssertionError("the server must never clone a client_attach product's source")

    async def checkout_new_branch(self, dest: Path, branch: str) -> None:
        raise AssertionError("there is no server-side checkout to branch")

    async def commit_all(self, dest: Path, message: str) -> bool:
        raise AssertionError("the run already committed, on the founder's machine")

    async def is_ahead_of_base(self, dest: Path, base: str) -> bool:
        raise AssertionError("there is no local checkout to compare — ask github")

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        raise AssertionError("the run already pushed, with the founder's own credential")


@dataclass
class _Runner:
    """Stands in for the plugin runner; records every action dispatched."""

    ahead_by: int = 3
    exists: bool = True
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def dispatch_action(self, plugin, *, action_name, context, kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((action_name, dict(kwargs)))
        if action_name == "compare_branch":
            return {"exists": self.exists, "ahead_by": self.ahead_by if self.exists else 0}
        return {"pr_number": 7, "url": "https://github.com/owner/name/pull/7"}

    @property
    def actions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, action: str) -> dict[str, Any]:
        return next(kw for name, kw in self.calls if name == action)


async def _seed(sf, cipher: _RoundtripCipher, *, execution_target: str) -> tuple[Any, Any, Any]:
    """A workspace with a github credential, a product, and a finished run."""
    from backend.connectors.auth import store
    from backend.connectors.auth.tokenset import TokenSet
    from backend.identity.workspaces_db import ProductRow

    ws = uuid.uuid4()
    product_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sf() as s:
        account = ConnectorAccountRow(
            workspace_id=ws,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("legacy"),
            delivery_config={"repo": "owner/name"},
            is_active=True,
        )
        s.add(account)
        s.add(
            ProductRow(
                id=product_id,
                workspace_id=ws,
                slug=f"p-{product_id.hex[:6]}",
                name="p",
                repo_url="https://github.com/owner/name",
                product_metadata={"execution_target": execution_target},
            )
        )
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                product_id=product_id,
                status=RunStatus.REVIEW_READY,
                payload={"intent_text": "do the thing"},
            )
        )
        await s.flush()
        await store.upsert_token(
            s,
            connector_account_id=account.id,
            provider="github",
            token=TokenSet(access_token=_TOKEN, refresh_token=None, expires_at=None),
            cipher=cipher,  # type: ignore[arg-type]
        )
        await s.commit()
        s.expunge(account)
    return ws, run_id, account


def _deps(sf, cipher: _RoundtripCipher, runner: _Runner) -> GithubDeliveryDeps:
    return GithubDeliveryDeps(
        cipher=cipher,  # type: ignore[arg-type]
        plugins_by_name={"github": object()},  # type: ignore[dict-item]
        # None on purpose: a client_attach run has no server-side workspace, and
        # requiring one is exactly what made this path unreachable.
        workspace_root=None,
        git_ops=_ForbiddenGitOps(),  # type: ignore[arg-type]
        remote_url_for=lambda repo: f"https://example.invalid/{repo}.git",
        runner=runner,  # type: ignore[arg-type]
        session_factory=sf,
    )


async def test_the_runs_pushed_branch_becomes_a_pull_request() -> None:
    cipher = _RoundtripCipher()
    runner = _Runner()
    async with shared_file_sessionmaker() as sf:
        ws, run_id, account = await _seed(sf, cipher, execution_target="client_attach")
        actions = await deliver_github(
            deps=_deps(sf, cipher, runner),
            binding=GithubBinding(account=account, repo="owner/name", base_branch="main"),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "Add the thing\n\nDetails."},
        )

    assert actions[0].succeeded is True, actions[0].error
    assert "open_pr" in runner.actions
    opened = runner.kwargs_for("open_pr")
    assert opened["head"] == worktree_branch(run_id), (
        "the PR must come off the branch the RUN pushed (#735), not the server-side "
        f"delivery branch that was never created: {opened['head']!r}"
    )
    assert opened["base"] == "main"
    assert opened["title"] == "Add the thing"


async def test_a_run_that_pushed_nothing_opens_no_pull_request() -> None:
    """The server-side rule "no changes → no PR" survives the move.

    Asked of GITHUB rather than of a local flag: the run's own record of what it
    pushed can disagree with what actually landed (a push that failed after the
    commit, an earlier attempt's branch). The remote is the authority on whether
    a PR can be opened at all, and an empty PR is a claim that work happened.
    """
    cipher = _RoundtripCipher()
    runner = _Runner(exists=False)
    async with shared_file_sessionmaker() as sf:
        ws, run_id, account = await _seed(sf, cipher, execution_target="client_attach")
        actions = await deliver_github(
            deps=_deps(sf, cipher, runner),
            binding=GithubBinding(account=account, repo="owner/name", base_branch="main"),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "Nothing to do"},
        )

    assert actions[0].succeeded is True, "a run that changed nothing is not a delivery FAILURE"
    assert "open_pr" not in runner.actions
    assert actions[0].output == {"skipped": True, "reason": "no_changes"}


async def test_a_branch_level_with_base_opens_no_pull_request() -> None:
    """Present but not ahead: github answers ``open_pr`` with 422 "No commits
    between", which would surface as a delivery failure for a run that simply
    had nothing to add."""
    cipher = _RoundtripCipher()
    runner = _Runner(ahead_by=0)
    async with shared_file_sessionmaker() as sf:
        ws, run_id, account = await _seed(sf, cipher, execution_target="client_attach")
        actions = await deliver_github(
            deps=_deps(sf, cipher, runner),
            binding=GithubBinding(account=account, repo="owner/name", base_branch="main"),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "s"},
        )

    assert actions[0].succeeded is True
    assert "open_pr" not in runner.actions


async def test_the_credential_is_the_servers_and_the_pr_carries_it() -> None:
    """The founder's machine pushed with THEIR git credential; the server never
    sent them a token (#735). Opening the PR is the other side of that split —
    an API call the server makes with the credential it already holds."""
    cipher = _RoundtripCipher()
    captured: dict[str, Any] = {}

    class _TokenRunner(_Runner):
        async def dispatch_action(self, plugin, *, action_name, context, kwargs):  # type: ignore[no-untyped-def]
            captured[action_name] = context.credentials.get("token")
            return await super().dispatch_action(
                plugin, action_name=action_name, context=context, kwargs=kwargs
            )

    runner = _TokenRunner()
    async with shared_file_sessionmaker() as sf:
        ws, run_id, account = await _seed(sf, cipher, execution_target="client_attach")
        await deliver_github(
            deps=_deps(sf, cipher, runner),
            binding=GithubBinding(account=account, repo="owner/name", base_branch="main"),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            run_id=run_id,
            content={"summary": "s"},
        )

    assert captured.get("open_pr") == _TOKEN
