"""ModelAccountResolver — rule + workspace-default + hard-fail (Lift E2)."""

from __future__ import annotations

from typing import Any

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
    """A rule that maps caller_id='workflow.frame' to the cloud account
    via the canonical column shape."""
    rule = RunRoutingRuleRow(
        workspace_id=workspace.id,
        name="frame -> cloud",
        caller_id=CALLER_FRAME,
        priority=10,
        is_default=False,
        target=cloud_account.litellm_model,
        conditions=[],
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    return rule


@pytest_asyncio.fixture
async def legacy_condition_rule(
    session: AsyncSession,
    workspace: WorkspaceRow,
    cloud_account: ModelAccount,
) -> RunRoutingRuleRow:
    """A rule that carries the caller_id only in the back-compat condition shape."""
    rule = RunRoutingRuleRow(
        workspace_id=workspace.id,
        name="frame -> cloud (legacy)",
        caller_id=None,
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

    async def test_canonical_column_rule_picks_target_account(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
        rule_caller_frame: RunRoutingRuleRow,
    ) -> None:
        workspace.default_account_id = model_account.id
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings())
        resolved = await resolver.resolve_for(
            caller_id=CALLER_FRAME,
            workspace_id=workspace.id,
        )
        assert resolved.account.id == cloud_account.id
        assert resolved.source == "explicit_rule"
        assert isinstance(resolved.adapter, ModelAccountAdapter)

    async def test_legacy_condition_clause_still_matches(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
        legacy_condition_rule: RunRoutingRuleRow,
    ) -> None:
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.account.id == cloud_account.id
        assert resolved.source == "explicit_rule"

    async def test_rule_for_different_caller_does_not_match(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        rule = RunRoutingRuleRow(
            workspace_id=workspace.id,
            name="other",
            caller_id="workflow.judge",
            priority=10,
            is_default=False,
            target=cloud_account.litellm_model,
            conditions=[],
            is_active=True,
        )
        session.add(rule)
        workspace.default_account_id = model_account.id
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings())
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
            caller_id=CALLER_FRAME,
            priority=10,
            is_default=False,
            target=cloud_account.litellm_model,
            conditions=[],
            is_active=False,
        )
        session.add(rule)
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
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
        resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.account.id == model_account.id
        assert resolved.source == "workspace_default"

    async def test_stale_default_pointing_at_inactive_account_falls_through(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
    ) -> None:
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
        model_account: ModelAccount,  # noqa: ARG002 — seeded but no default set
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


class TestPerCallerTimeoutFlow:
    """Lift E9 — resolver surfaces the per-caller timeout on ResolvedAccount."""

    async def test_resolved_account_carries_caller_default_timeout(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
    ) -> None:
        """``CALLER_FRAME`` has 300 s default (Lift E14 — bumped from 180 s
        after dogfood found one stuck big-file chunk wasted hours). The
        ResolvedAccount surfaces it so observability + future routing-rule
        overrides can read it without re-walking the registry."""
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.timeout_s == 300.0

    async def test_adapter_closes_over_timeout_at_construction(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
    ) -> None:
        """The adapter the resolver returns has ``timeout_s`` set so
        :meth:`chat` doesn't have to re-walk the caller registry per call."""
        from backend.dispatch.adapter import LiteLLMAdapter

        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert isinstance(resolved.adapter, LiteLLMAdapter)
        assert resolved.adapter.timeout_s == 300.0


class TestDefaultCatchAllRule:
    async def test_default_rule_catches_unmatched_caller(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        # Default rule with no caller_id + no conditions = catch-all.
        rule = RunRoutingRuleRow(
            workspace_id=workspace.id,
            name="catch-all",
            caller_id=None,
            priority=100,
            is_default=True,
            target=cloud_account.litellm_model,
            conditions=[],
            is_active=True,
        )
        session.add(rule)
        workspace.default_account_id = model_account.id
        await session.flush()
        resolver = ModelAccountResolver(session, settings=get_settings())
        resolved = await resolver.resolve_for(caller_id=CALLER_FRAME, workspace_id=workspace.id)
        assert resolved.account.id == cloud_account.id
        assert resolved.source == "explicit_rule"


class TestResolverConditionEvaluation:
    """#368 — the resolver MUST evaluate run-routing rule conditions
    (stage/pipeline/…) against the run, not just match caller_id. A rule
    whose condition doesn't match the run's context must be skipped."""

    async def test_condition_mismatch_skips_rule_falls_to_default(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        from backend.dispatch.caller_registry import CALLER_AGENT_LOOP_ACT
        from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

        # Default = model_account; an act-caller rule targets cloud_account
        # ONLY when pipeline == design_then_impl.
        workspace.default_account_id = model_account.id
        session.add(
            RunRoutingRuleRow(
                workspace_id=workspace.id,
                name="design -> cloud",
                caller_id=CALLER_AGENT_LOOP_ACT,
                priority=0,
                is_default=False,
                target=cloud_account.litellm_model,
                conditions=[{"field": "pipeline", "operator": "eq", "value": "design_then_impl"}],
                is_active=True,
            )
        )
        # The run is a SINGLE-pipeline run → the rule's condition must NOT match.
        run = ExecutionRun(
            workspace_id=workspace.id,
            status=RunStatus.RUNNING,
            payload={"frame": {"pipeline": "single"}},
        )
        session.add(run)
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
        resolved = await resolver.resolve_for(
            caller_id=CALLER_AGENT_LOOP_ACT, workspace_id=workspace.id
        )
        # Condition didn't match → rule skipped → workspace default.
        assert resolved.account.id == model_account.id
        assert resolved.source == "workspace_default"

    async def test_condition_match_selects_rule_target(
        self,
        session: AsyncSession,
        workspace: WorkspaceRow,
        model_account: ModelAccount,
        cloud_account: ModelAccount,
    ) -> None:
        from backend.dispatch.caller_registry import CALLER_AGENT_LOOP_ACT
        from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

        workspace.default_account_id = model_account.id
        session.add(
            RunRoutingRuleRow(
                workspace_id=workspace.id,
                name="design -> cloud",
                caller_id=CALLER_AGENT_LOOP_ACT,
                priority=0,
                is_default=False,
                target=cloud_account.litellm_model,
                conditions=[{"field": "pipeline", "operator": "eq", "value": "design_then_impl"}],
                is_active=True,
            )
        )
        run = ExecutionRun(
            workspace_id=workspace.id,
            status=RunStatus.RUNNING,
            payload={"frame": {"pipeline": "design_then_impl"}},
        )
        session.add(run)
        await session.flush()

        resolver = ModelAccountResolver(session, settings=get_settings(), run_id=run.id)
        resolved = await resolver.resolve_for(
            caller_id=CALLER_AGENT_LOOP_ACT, workspace_id=workspace.id
        )
        # Condition matched → rule's target wins over the default.
        assert resolved.account.id == cloud_account.id
        assert resolved.source == "explicit_rule"
