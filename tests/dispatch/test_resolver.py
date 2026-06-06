"""ModelAccountResolver — rule + workspace-default + hard-fail."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.dispatch.adapter import ModelAccountAdapter
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.dispatch.resolver import (
    ModelAccountResolver,
    NoAdapterMethodError,
    NoMatchingRouteError,
)
from backend.identity.workspaces_db import WorkspaceRow
from backend.router.accounts.models import ModelAccount
from backend.router.routing.run_routing.db import RunRoutingRuleRow


@pytest_asyncio.fixture
async def rule_caller_frame(
    session: AsyncSession,
    workspace: WorkspaceRow,
    cloud_account: ModelAccount,
) -> RunRoutingRuleRow:
    """A rule that maps caller_id='workflow.frame' to the cloud account."""
    rule = RunRoutingRuleRow(
        workspace_id=workspace.id,
        name="frame -> cloud",
        priority=10,
        is_default=False,
        target=cloud_account.litellm_model,
        conditions=[{"field": "caller_id", "operator": "eq", "value": CALLER_FRAME}],
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    return rule


class TestResolverRuleMatch:
    """Explicit rule beats workspace default."""

    async def test_active_rule_picks_target_account(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
        rule_caller_frame: RunRoutingRuleRow,
    ) -> None:
        # Default points elsewhere — rule must still win.
        workspace.default_account_id = model_account.id
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings())
        with patch("backend.dispatch.adapter.build_gateway_dispatcher") as mock_build:
            mock_build.return_value = None  # adapter ignores it in tests
            resolved = await resolver.resolve_for(
                caller_id=CALLER_FRAME,
                workspace_id=workspace.id,
            )
        assert resolved.account.id == cloud_account.id
        assert resolved.source == "explicit_rule"
        assert isinstance(resolved.adapter, ModelAccountAdapter)

    async def test_rule_without_caller_id_does_not_match(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        # Rule that targets a different field — should be skipped.
        rule = RunRoutingRuleRow(
            workspace_id=workspace.id,
            name="other",
            priority=10,
            is_default=False,
            target=cloud_account.litellm_model,
            conditions=[{"field": "stage", "operator": "eq", "value": "impl"}],
            is_active=True,
        )
        session.add(rule)
        workspace.default_account_id = model_account.id
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings())
        with patch("backend.dispatch.adapter.build_gateway_dispatcher", return_value=None):
            resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        # Falls back to workspace default.
        assert resolved.account.id == model_account.id
        assert resolved.source == "workspace_default"

    async def test_inactive_rule_is_skipped(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        rule = RunRoutingRuleRow(
            workspace_id=workspace.id,
            name="frame -> cloud (off)",
            priority=10,
            is_default=False,
            target=cloud_account.litellm_model,
            conditions=[{"field": "caller_id", "operator": "eq", "value": CALLER_FRAME}],
            is_active=False,
        )
        session.add(rule)
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        with patch("backend.dispatch.adapter.build_gateway_dispatcher", return_value=None):
            resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.source == "workspace_default"


class TestResolverWorkspaceDefault:
    async def test_default_account_used_when_no_rule(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
    ) -> None:
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        with patch("backend.dispatch.adapter.build_gateway_dispatcher", return_value=None):
            resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.account.id == model_account.id
        assert resolved.source == "workspace_default"

    async def test_stale_default_pointing_at_inactive_account_falls_through(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
    ) -> None:
        # Mark the account inactive — default should be treated as unset.
        model_account.is_active = False
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        with pytest.raises(NoMatchingRouteError):
            await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)


class TestResolverHardFail:
    async def test_no_rule_no_default_raises(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,  # noqa: ARG002 — seeded so the workspace has one but no default set
    ) -> None:
        resolver = ModelAccountResolver(session, settings=get_settings())
        with pytest.raises(NoMatchingRouteError) as exc_info:
            await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert exc_info.value.caller_id == CALLER_FRAME
        assert exc_info.value.workspace_id == workspace.id

    async def test_unknown_caller_id_raises_key_error(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
    ) -> None:
        resolver = ModelAccountResolver(session, settings=get_settings())
        with pytest.raises(KeyError):
            await resolver.resolve_for(caller_id="not.a.real.caller", workspace_id=workspace.id)


class TestResolverDefensiveValidation:
    async def test_check_supported_raises_on_missing_method(self) -> None:
        from backend.dispatch.caller_registry import CallerSpec

        spec = CallerSpec(
            caller_id="needs.both",
            required_methods=frozenset({"chat", "execute"}),
            description="x",
        )

        class _OnlyChat:
            supported_methods = frozenset({"chat"})

            async def chat(
                self, *, system: str, messages: list[dict[str, Any]], tools: Any = None
            ) -> Any: ...

        with pytest.raises(NoAdapterMethodError):
            ModelAccountResolver._check_supported(spec, _OnlyChat())  # type: ignore[arg-type]
