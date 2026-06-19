"""ModelAccountResolver — caller_id × workspace_id → account.

The resolver is mechanism-only: it does not decide policy. It looks for
a match in this order:

1. **Explicit rule** — an active
   :class:`~backend.router.routing.run_routing.db.RunRoutingRuleRow`
   whose ``conditions`` carry a ``caller_id`` equality clause that
   matches. When found, the rule's ``target`` (a ``litellm_model``)
   picks the workspace's ACTIVE account that publishes that model.
2. **Workspace default** — :attr:`WorkspaceRow.default_account_id`. The
   founder sets this through Settings (PWA) or the MCP tool. The
   resolver never auto-stamps it.
3. **Hard fail** — :class:`NoMatchingRouteError`. The call site surfaces
   the error to the user / PWA Settings instead of silently picking a
   model.

After Lift E2 there is no classifier path, no tier vocabulary, no
provider allow-list. Dispatch flows through this resolver alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.dispatch.adapter import ModelAccountAdapter, adapter_for
from backend.dispatch.caller_registry import (
    CallerSpec,
    get_caller_spec,
)
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.service import ModelAccountService
from backend.router.routing.run_routing.db import RunRoutingRuleRow

logger = structlog.get_logger(__name__)

__all__ = [
    "NoAdapterMethodError",
    "NoMatchingRouteError",
    "ResolvedAccount",
    "ModelAccountResolver",
]


class NoMatchingRouteError(Exception):
    """No rule matched AND the workspace has no ``default_account_id``.

    The call site MUST surface this rather than silently fall back to a
    different model. The PWA renders the error as "no model account is
    configured for this caller" with a deep link into Settings → Models.
    """

    def __init__(self, *, caller_id: str, workspace_id: uuid.UUID) -> None:
        super().__init__(
            f"no routing rule matched + no workspace default for caller "
            f"{caller_id!r} (workspace={workspace_id})"
        )
        self.caller_id = caller_id
        self.workspace_id = workspace_id


class NoAdapterMethodError(Exception):
    """The matched adapter does not support every method the caller needs.

    Rule creation should catch this at write time (the validator lives in
    the rules service); the resolver still raises defensively in case a
    rule was created before the spec was tightened.
    """

    def __init__(self, *, caller_id: str, missing: frozenset[str]) -> None:
        super().__init__(f"adapter missing methods {sorted(missing)!r} for caller {caller_id!r}")
        self.caller_id = caller_id
        self.missing = missing


@dataclass(frozen=True, slots=True)
class ResolvedAccount:
    """The bundle a call site receives — account + adapter + provenance.

    ``timeout_s`` (Lift E9) is the per-caller chat-timeout override taken
    from :attr:`CallerSpec.default_timeout_s`. ``None`` means the call
    site should fall back to ``settings.executor_task_timeout_s`` (the
    legacy single global). Callers that pre-cache the adapter (most do)
    rely on the adapter itself already having the timeout closed over —
    this field exists so observability + future routing-rule overrides
    can read the resolved value without re-walking the registry.
    """

    account: ModelAccount
    adapter: ModelAccountAdapter
    source: str  # "explicit_rule" | "workspace_default"
    timeout_s: float | None = None


class ModelAccountResolver:
    """Resolve a :class:`ResolvedAccount` for ``(caller_id, workspace_id)``."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        accounts: ModelAccountService | None = None,
        cipher: CredentialCipher | None = None,
        skill_names: list[str] | None = None,
        redis: Any = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        run_id: uuid.UUID | None = None,
        repo_url: str | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        # Lazy cipher / accounts service — only built when we actually
        # need to decrypt an api key (i.e. when a route resolves). A
        # workspace with no rules + no default never touches them, so
        # tests/dev environments that lack ``BSVIBE_GATEWAY_KMS_KEY_B64``
        # don't crash at resolver construction time.
        self._cipher = cipher
        self._accounts = accounts
        self._skill_names = skill_names or []
        # Redis is threaded into the ExecutorAdapter so the worker stream
        # XADD has a transport (Lift E3). LiteLLM accounts never touch
        # it; an executor account with no redis raises
        # :class:`~backend.dispatch.adapter.ExecutorAdapterUnavailable`
        # at chat() time.
        self._redis = redis
        # Lift E19 — optional ``async_sessionmaker`` plumbed into the
        # ExecutorAdapter so each ``chat`` call opens its OWN
        # ``AsyncSession`` for the dispatch lifecycle. Without this, the
        # bootstrap / settle path passes one session down to every
        # parallel chunk of ``IngestCompiler.compile_batch``, and two
        # concurrent chunks hit the "Session is already flushing" guard.
        # ``None`` falls back to the bound ``session`` (legacy).
        self._session_factory = session_factory
        # Lift E31 — when the agent_loop builds a resolver for one run, it
        # passes the run id so the ExecutorAdapter the resolver hands back
        # records its dispatched task under that run. Files captured by the
        # worker then land as the run's ``artifact_refs`` instead of being
        # silently dropped (the chat-shaped tasks' pre-E31 behaviour).
        self._run_id = run_id
        # Lift E32 — when the agent_loop knows the run's product, it passes
        # the product's repo URL so the ExecutorAdapter the resolver hands
        # back tells the worker to clone the repo into the per-task
        # workspace before invoking the executor.
        self._repo_url = repo_url

    def _ensure_accounts(self) -> ModelAccountService:
        if self._accounts is not None:
            return self._accounts
        if self._cipher is None:
            self._cipher = CredentialCipher(_key_from_settings())
        self._accounts = ModelAccountService(self._session, cipher=self._cipher)
        return self._accounts

    async def resolve_for(
        self,
        *,
        caller_id: str,
        workspace_id: uuid.UUID,
    ) -> ResolvedAccount:
        """Resolve the account for one ``(caller_id, workspace_id)`` pair.

        Raises :class:`NoMatchingRouteError` when nothing matches.
        Raises :class:`KeyError` for an unknown ``caller_id``.
        """
        # Validate the caller is known — mistyped ids should never reach
        # the rule matcher.
        spec = get_caller_spec(caller_id, skill_names=self._skill_names)

        # 1. Explicit rule — first match by priority.
        account = await self._match_rule(caller_id, workspace_id)
        source = "explicit_rule"

        # 2. Workspace default fallback.
        if account is None:
            account = await self._workspace_default(workspace_id)
            source = "workspace_default"

        # 3. Nothing → hard fail.
        if account is None:
            logger.info(
                "dispatch_resolve_no_match",
                caller_id=caller_id,
                workspace_id=str(workspace_id),
            )
            raise NoMatchingRouteError(caller_id=caller_id, workspace_id=workspace_id)

        # Executor accounts never carry an api key (CLI subprocess uses
        # the host's own credential); skip the decryption entirely so a
        # workspace that registered only an executor account doesn't
        # require ``BSVIBE_GATEWAY_KMS_KEY_B64`` just to resolve.
        from backend.router.accounts.predicates import (  # noqa: PLC0415
            is_executor_account,
        )

        if is_executor_account(account):
            api_key = ""
        else:
            api_key = self._ensure_accounts().reveal_api_key(account)
        adapter = adapter_for(
            account,
            session=self._session,
            settings=self._settings,
            api_key=api_key,
            redis=self._redis,
            # Lift E9 — close the per-caller timeout into the adapter so
            # ``chat`` doesn't re-walk the registry per call.
            timeout_s=spec.default_timeout_s,
            # Lift E19 — when the runtime wired a sessionmaker, the
            # ExecutorAdapter uses it to open a fresh session per chat
            # call so parallel chunks don't race on flush().
            session_factory=self._session_factory,
            # Lift E31 — when the resolver was wired with the run id,
            # the ExecutorAdapter binds its dispatched task to that run
            # so worker-captured files land as the run's artifact_refs.
            run_id=self._run_id,
            # Lift E32 — when the resolver was wired with a repo URL,
            # the ExecutorAdapter dispatches it so the worker clones the
            # repo into the per-task workspace before calling the executor.
            repo_url=self._repo_url,
        )

        # Defensive validation — rule creation is supposed to catch this
        # at write time, but a workspace_default fallback bypasses the
        # rule validator so we re-check here.
        self._check_supported(spec, adapter)

        logger.info(
            "dispatch_resolve_hit",
            caller_id=caller_id,
            workspace_id=str(workspace_id),
            source=source,
            account_id=str(account.id),
            provider=account.provider,
            litellm_model=account.litellm_model,
            timeout_s=spec.default_timeout_s,
        )
        return ResolvedAccount(
            account=account,
            adapter=adapter,
            source=source,
            timeout_s=spec.default_timeout_s,
        )

    # ----- internals -----

    async def _match_rule(self, caller_id: str, workspace_id: uuid.UUID) -> ModelAccount | None:
        """Resolve the rule-selected account via the run-routing ENGINE.

        Issue #368 — this used to match ``caller_id`` ONLY and ignore each
        rule's ``conditions`` (stage / pipeline / path_classification / …),
        so condition-based routing (the design→codex / impl→opencode
        stage split) silently did nothing. We now delegate to
        :func:`evaluate_rules`, which matches caller_id AND evaluates the
        conditions against the run's :class:`RoutingContext` (built from
        ``self._run_id``). Both write shapes — the canonical ``caller_id``
        column and the back-compat caller_id condition clause — are honoured
        by the engine. ``evaluate_rules`` also returns the active default
        rule's target when no explicit rule matches.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from backend.router.routing.run_routing.engine import (  # noqa: PLC0415
            evaluate_rules,
        )

        stmt = (
            select(RunRoutingRuleRow)
            .where(RunRoutingRuleRow.workspace_id == workspace_id)
            .where(RunRoutingRuleRow.is_active.is_(True))
            .order_by(RunRoutingRuleRow.priority.asc())
        )
        rules = list((await self._session.execute(stmt)).scalars().all())
        if not rules:
            return None

        ctx = await self._build_routing_context(caller_id)
        target = evaluate_rules(rules, ctx)
        if target is None:
            return None
        return await self._account_for_target(workspace_id, target)

    async def _build_routing_context(self, caller_id: str) -> Any:
        """Build the :class:`RoutingContext` the engine evaluates conditions
        against — from the run (``self._run_id``) + the dispatch ``caller_id``.

        No run bound (settle / bootstrap callers) → a minimal context carrying
        just the caller_id (stage/pipeline default to ``"single"``), so
        caller-only rules still match and stage/pipeline conditions simply
        don't."""
        from dataclasses import replace  # noqa: PLC0415

        from backend.router.routing.run_routing.engine import (  # noqa: PLC0415
            RoutingContext,
        )

        if self._run_id is None:
            return RoutingContext(caller_id=caller_id)
        from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

        run = await self._session.get(ExecutionRun, self._run_id)
        if run is None:
            return RoutingContext(caller_id=caller_id)
        return replace(RoutingContext.from_run(run), caller_id=caller_id)

    async def _account_for_target(
        self, workspace_id: uuid.UUID, target: str
    ) -> ModelAccount | None:
        from backend.router.infrastructure.repositories import (  # noqa: PLC0415
            SqlAlchemyModelAccountRepository,
        )

        repo = SqlAlchemyModelAccountRepository(self._session)
        accounts = await repo.list_active_for_workspace(workspace_id=workspace_id)
        for account in accounts:
            if account.litellm_model == target:
                return account
        # #368 — accept the natural ``executor/<executor_type>`` selector
        # (what the PWA/MCP rule forms suggest) in addition to a bare
        # ``litellm_model`` match. An executor account's litellm_model is the
        # vendor-prefixed model (e.g. ``opencode-go/qwen3.6-plus``), NOT
        # ``executor/opencode`` — so a target of ``executor/opencode`` would
        # otherwise resolve to no account and silently fall through.
        if target.startswith("executor/"):
            want_type = target.split("/", 1)[1]
            for account in accounts:
                if getattr(account, "provider", None) != "executor":
                    continue
                extra = account.extra_params if isinstance(account.extra_params, dict) else {}
                if extra.get("executor_type") == want_type:
                    return account
        return None

    async def _workspace_default(self, workspace_id: uuid.UUID) -> ModelAccount | None:
        from sqlalchemy import select  # noqa: PLC0415

        from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415

        stmt = select(WorkspaceRow.default_account_id).where(WorkspaceRow.id == workspace_id)
        default_id = (await self._session.execute(stmt)).scalar_one_or_none()
        if default_id is None:
            return None
        from backend.router.infrastructure.repositories import (  # noqa: PLC0415
            SqlAlchemyModelAccountRepository,
        )

        repo = SqlAlchemyModelAccountRepository(self._session)
        accounts = await repo.list_active_for_workspace(workspace_id=workspace_id)
        for account in accounts:
            if account.id == default_id:
                return account
        # Default points at a now-inactive / deleted account — treat as
        # unset rather than silently 500.
        logger.info(
            "dispatch_resolve_default_stale",
            workspace_id=str(workspace_id),
            default_account_id=str(default_id),
        )
        return None

    @staticmethod
    def _check_supported(spec: CallerSpec, adapter: ModelAccountAdapter) -> None:
        missing = spec.required_methods - adapter.supported_methods
        if missing:
            raise NoAdapterMethodError(caller_id=spec.caller_id, missing=missing)
