"""A delivery with no commits fails only when the artifact IS a code change.

#1266 made "the branch has no commits" a delivery FAILURE (``NO_CHANGES_ERROR``) so a
verified code change that opened no PR is not reported as shipped. But
``ConnectorDeliveryAdapter.dispatch`` calls the github delivery for any product with a
github binding, whatever the artifact — so a weekly report (``direct_output``), which
never promised a PR, became a ``delivery_failed`` decision although the report itself
reached the founder's telegram.

Everything here runs through the real ``dispatch``; only the github plugin calls
(``compare_branch`` / ``open_pr``), git and the telegram outbound are doubles.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.extensions.plugin.base import OutboundCapability, PluginMeta
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow
from backend.workflow.application.delivery.connector_dispatch import (
    build_connector_delivery_adapter,
)
from backend.workflow.application.delivery.connector_dispatch._github_in_place import (
    NO_CHANGES_ERROR,
)
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from tests._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio

_TOKEN = "ghu_oauth_access"  # noqa: S105 — test fixture


class _Cipher:
    def encrypt(self, plaintext: str) -> str:
        return f"ct:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("ct:")


@dataclass
class _GithubRunner:
    """Stands in for the github plugin: no commit landed on the branch."""

    calls: list[str] = field(default_factory=list)

    async def dispatch_action(self, plugin, *, action_name, context, kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(action_name)
        if action_name == "compare_branch":
            return {"exists": False, "ahead_by": 0}
        return {"pr_number": 7, "url": "https://github.com/owner/name/pull/7"}


class _EmptyBranchGitOps:
    """Server-side git where the agent changed nothing and the branch is level with base."""

    async def commit_all(self, dest: Path, message: str) -> bool:
        return False

    async def is_ahead_of_base(self, dest: Path, base: str) -> bool:
        return False

    async def push(self, dest: Path, branch: str, *, token: str | None) -> None:
        raise AssertionError("nothing to push")


class _TelegramRunner:
    """The ``PluginRunner`` the telegram outbound goes through (the dispatcher's)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def dispatch_outbound(self, meta, *, artifact_type, context, event):  # type: ignore[no-untyped-def]
        self.sent.append(dict(event))
        return {"ok": True}


def _telegram_plugin() -> PluginMeta:
    return PluginMeta(
        name="telegram",
        version="0",
        description="",
        author="t",
        credentials=[],
        outbounds=[
            OutboundCapability(fn=lambda *_a, **_k: None, artifact_types=("telegram_message",))
        ],
    )


async def _seed(
    sf, *, execution_target: str, deliverable_type: DeliverableType
) -> tuple[uuid.UUID, uuid.UUID]:
    """GitHub + telegram bound to one product, and a finished run's deliverable."""
    from backend.connectors.auth import store
    from backend.connectors.auth.tokenset import TokenSet

    cipher = _Cipher()
    ws, product_id, run_id, deliverable_id = (uuid.uuid4() for _ in range(4))
    async with sf() as s:
        github = ConnectorAccountRow(
            workspace_id=ws,
            connector="github",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("legacy"),
            delivery_config={"repo": "owner/name"},
            is_active=True,
        )
        telegram = ConnectorAccountRow(
            workspace_id=ws,
            connector="telegram",
            webhook_token=uuid.uuid4().hex,
            signing_secret_ciphertext=cipher.encrypt("bot-token"),
            delivery_config={"chat_id": "42"},
            is_active=True,
        )
        s.add_all([github, telegram])
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
                payload={"intent_text": "weekly report"},
            )
        )
        await s.flush()
        s.add(
            ResourceBindingRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                product_id=product_id,
                connector_account_id=telegram.id,
                resource_id=uuid.uuid4().hex,
                selection={},
            )
        )
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type=deliverable_type,
                payload={"summary": "Weekly report\n\nAll good."},
            )
        )
        await store.upsert_token(
            s,
            connector_account_id=github.id,
            provider="github",
            token=TokenSet(access_token=_TOKEN, refresh_token=None, expires_at=None),
            cipher=cipher,  # type: ignore[arg-type]
        )
        await s.commit()
    return ws, deliverable_id, run_id


async def _dispatch(
    sf, tmp_path: Path, *, execution_target: str, artifact_type: str
) -> tuple[Any, _GithubRunner, _TelegramRunner]:
    from backend.workflow.application.delivery.dispatcher import DeliveryDispatcher

    ws, deliverable_id, run_id = await _seed(
        sf,
        execution_target=execution_target,
        deliverable_type=DeliverableType(artifact_type),
    )
    (tmp_path / str(run_id)).mkdir()  # the server-side checkout
    telegram_runner = _TelegramRunner()
    adapter = build_connector_delivery_adapter(
        session_factory=sf,
        plugins=[_telegram_plugin(), PluginMeta("github", "0", "", "t", [], [])],
        cipher=_Cipher(),  # type: ignore[arg-type]
        dispatcher=DeliveryDispatcher(runner=telegram_runner),  # type: ignore[arg-type]
        workspace_root=tmp_path,
        git_ops=_EmptyBranchGitOps(),  # type: ignore[arg-type]
    )
    github_runner = _GithubRunner()
    adapter.runner = github_runner  # type: ignore[assignment]
    result = await adapter.dispatch(
        workspace_id=ws, deliverable_id=deliverable_id, artifact_type=artifact_type
    )
    return result, github_runner, telegram_runner


def _github_action(result: Any) -> Any:
    return next(a for a in result.actions if a.action.startswith("github:"))


def _telegram_action(result: Any) -> Any:
    return next(a for a in result.actions if a.action.startswith("telegram:"))


@pytest.mark.parametrize("execution_target", ["client_attach", "server"])
async def test_a_report_with_no_commits_is_a_clean_skip(
    tmp_path: Path, execution_target: str
) -> None:
    async with shared_file_sessionmaker() as sf:
        result, github, telegram = await _dispatch(
            sf, tmp_path, execution_target=execution_target, artifact_type="direct_output"
        )

    gh = _github_action(result)
    assert gh.succeeded is True, gh.error
    assert gh.output == {"skipped": True, "reason": "no_changes"}
    assert "open_pr" not in github.calls
    assert _telegram_action(result).succeeded is True
    assert len(telegram.sent) == 1, "the report still reaches telegram"
    assert result.error is None, "the report was delivered — no delivery_failed decision"


@pytest.mark.parametrize("artifact_type", ["code", "pr"])
@pytest.mark.parametrize("execution_target", ["client_attach", "server"])
async def test_a_code_change_with_no_commits_still_fails(
    tmp_path: Path, execution_target: str, artifact_type: str
) -> None:
    async with shared_file_sessionmaker() as sf:
        result, github, _telegram = await _dispatch(
            sf, tmp_path, execution_target=execution_target, artifact_type=artifact_type
        )

    gh = _github_action(result)
    assert gh.succeeded is False
    assert gh.error == NO_CHANGES_ERROR
    assert "open_pr" not in github.calls
    assert result.error == NO_CHANGES_ERROR
