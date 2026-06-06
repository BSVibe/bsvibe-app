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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dispatch.adapter import ModelAccountAdapter, adapter_for
from backend.dispatch.caller_registry import (
    SKILL_CALLER_PREFIX,
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
    """The bundle a call site receives — account + adapter + provenance."""

    account: ModelAccount
    adapter: ModelAccountAdapter
    source: str  # "explicit_rule" | "workspace_default"


# Condition the resolver looks for inside a rule's JSON ``conditions``
# array. The rule's row-level ``caller_id`` column is the primary
# matcher; this clause stays for back-compat with rules whose caller_id
# was authored as a condition before the column was added.
_CALLER_FIELD = "caller_id"


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
        )
        return ResolvedAccount(account=account, adapter=adapter, source=source)

    # ----- internals -----

    async def _match_rule(self, caller_id: str, workspace_id: uuid.UUID) -> ModelAccount | None:
        """First-active-rule wins among rules whose ``caller_id`` matches.

        Two write shapes are honoured:

        * The :class:`RunRoutingRuleRow.caller_id` column (the canonical
          shape after Lift E2 — rule creation requires it).
        * A ``{"field": "caller_id", "operator": "eq", "value": "..."}``
          entry inside ``conditions`` (the back-compat shape for rules
          authored before the column existed).
        """
        from sqlalchemy import select  # noqa: PLC0415

        stmt = (
            select(RunRoutingRuleRow)
            .where(RunRoutingRuleRow.workspace_id == workspace_id)
            .where(RunRoutingRuleRow.is_active.is_(True))
            .order_by(RunRoutingRuleRow.priority.asc())
        )
        rules = list((await self._session.execute(stmt)).scalars().all())
        if not rules:
            return None

        for rule in rules:
            if rule.is_default:
                continue
            if _rule_matches_caller(rule, caller_id):
                account = await self._account_for_target(workspace_id, rule.target)
                if account is not None:
                    return account
        # No explicit match — try the default rule (still a rule, just a
        # catch-all). We honour it ONLY when the rule actually targets a
        # live model account.
        for rule in rules:
            if rule.is_default and not rule.conditions and not rule.caller_id:
                account = await self._account_for_target(workspace_id, rule.target)
                if account is not None:
                    return account
        return None

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


def _matches_skill_short(value: str, caller_id: str) -> bool:
    """A skill caller_id ``skill.<name>`` also matches the bare ``<name>``."""
    return (
        caller_id.startswith(SKILL_CALLER_PREFIX) and value == caller_id[len(SKILL_CALLER_PREFIX) :]
    )


def _rule_matches_caller(rule: RunRoutingRuleRow, caller_id: str) -> bool:
    """True when ``rule`` matches ``caller_id``.

    Match precedence: ``rule.caller_id`` column (canonical) → ``conditions``
    back-compat clause. Skill caller_ids match either the full
    ``skill.<name>`` or the bare ``<name>``.
    """
    column = getattr(rule, "caller_id", None)
    if isinstance(column, str) and column:
        return column == caller_id or _matches_skill_short(column, caller_id)
    if not isinstance(rule.conditions, list):
        return False
    for clause in rule.conditions:
        if not isinstance(clause, dict):
            continue
        if clause.get("field") != _CALLER_FIELD:
            continue
        if clause.get("operator", "eq") != "eq":
            continue
        value = clause.get("value")
        if value == caller_id:
            return True
        if isinstance(value, str) and _matches_skill_short(value, caller_id):
            return True
    return False
