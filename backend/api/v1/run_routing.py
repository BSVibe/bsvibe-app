"""/api/v1/run-routing — author per-workspace RUN routing rules (Lift E2).

These rules pick which ModelAccount handles a run, keyed on the dispatch
caller_id + the run's framed signals. Lift E2 makes ``caller_id`` a
first-class column: every non-default rule must declare which caller
it routes, validated against
:mod:`backend.dispatch.caller_registry`. Default (catch-all) rules are
the only ones that may omit ``caller_id``.

Distinct from ``/api/v1/rules`` (the legacy LiteLLM-hook model rules;
unchanged by this lift).

Write-time validation:

* ``caller_id`` (when set) must be a known caller — either a static
  entry in :data:`backend.dispatch.caller_registry.KNOWN_CALLERS` or the
  workspace-managed ``skill.<name>`` namespace.
* ``conditions`` field/operator must be in the engine's
  :data:`ALLOWED_FIELDS` / :data:`VALID_OPERATORS`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.api.v1._router_deps import get_run_routing_rule_repository
from backend.dispatch.caller_registry import (
    CALLER_ROUTING_COMPILE,
    KNOWN_CALLERS,
    SKILL_CALLER_PREFIX,
    list_all_callers,
)
from backend.router.domain.repositories import RunRoutingRuleRepository
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import ALLOWED_FIELDS, VALID_OPERATORS
from backend.router.routing.run_routing.nl_compile import (
    CompiledCondition,
    RoutingCompileLlm,
    UninterpretableCondition,
    as_dicts,
    compile_rules,
    compile_source_text,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


def _validate_caller_id(value: str) -> str:
    """Reject a caller_id that isn't a static known caller or a
    well-formed ``skill.<name>`` id. The skill namespace is permissive
    here (we accept any well-formed name); the resolver does the final
    spec lookup against the per-workspace skill loader at dispatch time.
    """
    if value in KNOWN_CALLERS:
        return value
    if value.startswith(SKILL_CALLER_PREFIX) and len(value) > len(SKILL_CALLER_PREFIX):
        return value
    known = ", ".join(sorted(KNOWN_CALLERS))
    raise ValueError(f"unknown caller_id {value!r}; expected one of {{{known}}} or 'skill.<name>'")


class ConditionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: str = "eq"
    value: Any = None
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _field_allowed(cls, v: str) -> str:
        if v not in ALLOWED_FIELDS:
            allowed = ", ".join(sorted(ALLOWED_FIELDS))
            raise ValueError(f"unknown condition field {v!r}; allowed: {allowed}")
        return v

    @field_validator("operator")
    @classmethod
    def _operator_valid(cls, v: str) -> str:
        if v not in VALID_OPERATORS:
            raise ValueError(
                f"unknown operator {v!r}; allowed: {', '.join(sorted(VALID_OPERATORS))}"
            )
        return v


class RunRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    # Lift N5 — the founder's free-text NL CONDITION phrase. When set, the rule
    # is compiled FROM it (caller_id / conditions are derived, a category also
    # creates an intent def) and it is mutually exclusive with the structured
    # caller_id / conditions fields. ``None`` → the structured (back-compat) path.
    source_text: str | None = Field(default=None, min_length=1, max_length=500)
    # Lift E2 — required for any non-default STRUCTURED rule; ``None`` for the
    # catch-all default OR when source_text drives the rule. Validated against
    # the caller registry.
    caller_id: str | None = Field(default=None, max_length=120)
    priority: int = Field(default=0, ge=0)
    is_default: bool = False
    target: str = Field(min_length=1, max_length=255)
    conditions: list[ConditionPayload] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _shape_valid(self) -> RunRuleCreate:
        # NL path: source_text owns the caller_id/conditions — reject a mix so a
        # caller can't silently ship a half-structured rule.
        if self.source_text is not None:
            if self.caller_id or self.conditions or self.is_default:
                raise ValueError(
                    "source_text is mutually exclusive with caller_id / conditions / is_default; "
                    "provide a natural-language condition OR the structured fields, not both"
                )
            return self
        return self._require_structured_caller()

    def _require_structured_caller(self) -> RunRuleCreate:
        # Non-default rules must declare a caller_id (otherwise they'd
        # match nothing through the resolver's column-first matcher).
        # Back-compat: the row may still carry an in-conditions
        # caller_id clause — accept that shape too so a founder can
        # author the convenience condition form.
        if self.is_default:
            return self
        if self.caller_id:
            return self
        condition_callers = [
            c
            for c in self.conditions
            if c.field == "caller_id" and c.operator == "eq" and isinstance(c.value, str)
        ]
        if not condition_callers:
            raise ValueError(
                "non-default run-routing rules must declare a caller_id "
                "(either the top-level field or a {field:'caller_id', operator:'eq'} condition)"
            )
        return self


class RunRuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    caller_id: str | None = None
    source_text: str | None = None
    priority: int
    is_default: bool
    target: str
    conditions: list[dict[str, Any]]
    is_active: bool
    created_at: datetime


def _to_response(row: RunRoutingRuleRow) -> RunRuleResponse:
    return RunRuleResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        caller_id=row.caller_id,
        source_text=row.source_text,
        priority=row.priority,
        is_default=row.is_default,
        target=row.target,
        conditions=row.conditions if isinstance(row.conditions, list) else [],
        is_active=row.is_active,
        created_at=row.created_at,
    )


class RunCallerResponse(BaseModel):
    """One selectable caller for the PWA's run-routing rule form. Keeps the
    caller whitelist a single source of truth (the registry), so the UI never
    hardcodes it."""

    model_config = ConfigDict(extra="forbid")

    caller_id: str
    description: str


@router.get("/callers")
async def list_callers() -> list[RunCallerResponse]:
    """The static known callers a rule may target (skill.<name> callers are
    workspace-scoped and authored by typing the id, so they're not listed)."""
    return [
        RunCallerResponse(caller_id=spec.caller_id, description=spec.description)
        for spec in list_all_callers()
    ]


@router.get("")
async def list_run_rules(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
) -> list[RunRuleResponse]:
    rows = await rules.list_by_workspace(workspace_id=workspace_id)
    return [_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# NL condition → single-rule structure (Lift N5).
#
# A rule can be authored from ONE free-text condition phrase + a target. The
# phrase compiles — per single rule — into the structured caller_id / conditions;
# a category also creates an intent def (under the personal account) so the N1
# classifier has something to match. An uninterpretable phrase raises so the
# endpoint 422s rather than persisting a dead rule.
# ---------------------------------------------------------------------------


class SourceTextUninterpretableError(Exception):
    """The NL condition phrase compiled to nothing valid — the endpoint 422s and
    persists no rule."""


async def compile_source_text_for_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    text: str,
    *,
    llm: RoutingCompileLlm | None = None,
) -> CompiledCondition:
    """Compile ONE NL condition ``text`` into a validated single dimension.

    Gathers the caller catalog + resolves the compiler's own model (unless
    ``llm`` is injected for tests). Raises :class:`NoCompileModelError` when no
    model is configured to compile on, and
    :class:`SourceTextUninterpretableError` when the phrase compiles to nothing
    valid. Shared by the REST + MCP create/update paths."""
    callers = [(spec.caller_id, spec.description) for spec in list_all_callers()]
    if llm is None:
        llm = await _resolve_compile_llm(session, workspace_id)
        if llm is None:
            raise NoCompileModelError
    result = await compile_source_text(text, callers=callers, llm=llm)
    if isinstance(result, UninterpretableCondition):
        raise SourceTextUninterpretableError(text)
    return result


async def _conditions_from_compiled(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    compiled: CompiledCondition,
) -> list[dict[str, Any]]:
    """Materialise the rule's ``conditions`` from a compiled dimension.

    For a **category** this creates the intent definition (name + seed examples,
    embedded via the account's :class:`EmbeddingService`) first, then keys the
    rule on ``classified_intent == intent_name``. Flushes but does not commit —
    the caller owns the transaction so intent + rule land atomically."""
    if compiled.intent_name is not None:
        from backend.embedding.authoring import (  # noqa: PLC0415
            build_account_embedder,
            create_intent_with_examples,
        )

        embedder = await build_account_embedder(
            session, workspace_id=workspace_id, account_id=account_id
        )
        await create_intent_with_examples(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            name=compiled.intent_name,
            threshold=0.65,
            examples=compiled.intent_examples or [],
            embedder=embedder,
        )
    if compiled.condition is not None:
        return [{**compiled.condition, "negate": False}]
    return []


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run_rule(
    payload: RunRuleCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRuleResponse:
    caller_id = payload.caller_id
    conditions = [c.model_dump() for c in payload.conditions]

    if payload.source_text is not None:
        try:
            compiled = await compile_source_text_for_workspace(
                session, workspace_id, payload.source_text
            )
        except NoCompileModelError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "no model is configured to compile the condition with — set a "
                    "default model or add a model account first"
                ),
            ) from None
        except SourceTextUninterpretableError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"could not interpret the condition {payload.source_text!r} as a routing "
                    "rule — try rephrasing (e.g. '복잡한 작업', '마케팅 관련', '한국어 요청')"
                ),
            ) from None
        caller_id = compiled.caller_id
        conditions = await _conditions_from_compiled(
            session, workspace_id=workspace_id, account_id=account_id, compiled=compiled
        )

    row = RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        caller_id=caller_id,
        source_text=payload.source_text,
        priority=payload.priority,
        is_default=payload.is_default,
        target=payload.target,
        conditions=conditions,
        is_active=payload.is_active,
    )
    try:
        await rules.add(row)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a run-routing rule named {payload.name!r} already exists",
        ) from None
    logger.info(
        "run_routing_rule_created",
        workspace_id=str(workspace_id),
        name=payload.name,
        caller_id=caller_id,
        from_source_text=payload.source_text is not None,
    )
    return _to_response(row)


class RunRuleUpdate(BaseModel):
    """Partial edit of a run-routing rule (Lift 6 + N5). The user-facing knobs:
    the NL ``source_text`` condition (recompiled + rewrites caller_id/conditions
    on save), which caller it routes, the target model, and the active toggle."""

    model_config = ConfigDict(extra="forbid")

    source_text: str | None = Field(default=None, min_length=1, max_length=500)
    caller_id: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _source_text_not_mixed_with_caller(self) -> RunRuleUpdate:
        if self.source_text is not None and self.caller_id is not None:
            raise ValueError(
                "source_text and caller_id are mutually exclusive on update — "
                "editing source_text recompiles the caller_id/conditions"
            )
        return self


@router.patch("/{rule_id}")
async def update_run_rule(
    rule_id: uuid.UUID,
    payload: RunRuleUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRuleResponse:
    row = await rules.get(workspace_id=workspace_id, rule_id=rule_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    if payload.source_text is not None:
        # Recompile the NL condition → rewrite caller_id/conditions (an
        # uninterpretable phrase 422s and leaves the rule untouched).
        try:
            compiled = await compile_source_text_for_workspace(
                session, workspace_id, payload.source_text
            )
        except NoCompileModelError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "no model is configured to compile the condition with — set a "
                    "default model or add a model account first"
                ),
            ) from None
        except SourceTextUninterpretableError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"could not interpret the condition {payload.source_text!r} as a routing "
                    "rule — try rephrasing (e.g. '복잡한 작업', '마케팅 관련', '한국어 요청')"
                ),
            ) from None
        row.source_text = payload.source_text
        row.caller_id = compiled.caller_id
        row.conditions = await _conditions_from_compiled(
            session, workspace_id=workspace_id, account_id=account_id, compiled=compiled
        )
    if payload.caller_id is not None:
        row.caller_id = payload.caller_id
    if payload.target is not None:
        row.target = payload.target
    if payload.is_active is not None:
        row.is_active = payload.is_active
    await session.commit()
    logger.info("run_routing_rule_updated", workspace_id=str(workspace_id), rule_id=str(rule_id))
    return _to_response(row)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_rule(
    rule_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await rules.get(workspace_id=workspace_id, rule_id=rule_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    await rules.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# NL → rules compiler (Lift 5) — dry-run: returns proposals, never persists.
# ---------------------------------------------------------------------------


class NoCompileModelError(Exception):
    """No model is configured to run the NL compiler on (no route for the
    ``routing.compile`` caller + no workspace default). The founder must set a
    default model or add an account before the compiler can run."""


class _AdapterCompileLlm:
    """Bridges a resolved dispatch adapter to the compiler's ``complete_text``
    seam — one ``(system, user)`` → text chat call (no tools)."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def complete_text(self, *, system: str, user: str) -> str:
        response = await self._adapter.chat(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=None,
        )
        return str(response.content)


async def _resolve_compile_llm(
    session: AsyncSession, workspace_id: uuid.UUID
) -> RoutingCompileLlm | None:
    """Resolve the model the compiler runs ON via the same resolver everything
    else uses (caller ``routing.compile``). ``None`` when nothing is configured."""
    from backend.config import get_settings  # noqa: PLC0415
    from backend.dispatch.resolver import (  # noqa: PLC0415
        ModelAccountResolver,
        NoMatchingRouteError,
    )

    resolver = ModelAccountResolver(session, settings=get_settings())
    try:
        resolved = await resolver.resolve_for(
            caller_id=CALLER_ROUTING_COMPILE, workspace_id=workspace_id
        )
    except NoMatchingRouteError:
        return None
    return _AdapterCompileLlm(resolved.adapter)


async def compile_for_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    text: str,
    *,
    llm: RoutingCompileLlm | None = None,
) -> list[dict[str, Any]]:
    """Compile ``text`` into validated rule-proposal dicts for ``workspace_id``.

    Gathers the caller catalog (registry) + the workspace's active model accounts,
    resolves the compiler's own model (unless ``llm`` is injected for tests), and
    returns the create-endpoint wire shape for each proposal. Raises
    :class:`NoCompileModelError` when no model is configured to compile on. Shared
    by the REST endpoint and the MCP tool so both land on one compile path."""
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyModelAccountRepository,
    )

    callers = [(spec.caller_id, spec.description) for spec in list_all_callers()]
    accounts = await SqlAlchemyModelAccountRepository(session).list_active_for_workspace(
        workspace_id=workspace_id
    )
    targets = [(a.label, a.litellm_model) for a in accounts]

    if llm is None:
        llm = await _resolve_compile_llm(session, workspace_id)
        if llm is None:
            raise NoCompileModelError
    rules = await compile_rules(text, callers=callers, targets=targets, llm=llm)
    return as_dicts(rules)


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class CompileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[dict[str, Any]]


@router.post("/compile")
async def compile_run_rules(
    payload: CompileRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CompileResponse:
    """Compile a plain-language routing description into rule PROPOSALS (dry-run —
    nothing is persisted; the caller previews then applies the ones it wants)."""
    try:
        proposals = await compile_for_workspace(session, workspace_id, payload.text)
    except NoCompileModelError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no model is configured to compile with — set a default model or "
                "add a model account first"
            ),
        ) from None
    return CompileResponse(proposals=proposals)


# ---------------------------------------------------------------------------
# Apply — persist the accepted proposals atomically (Lift N3).
# ---------------------------------------------------------------------------


class ApplyError(Exception):
    """A proposal could not be applied (e.g. its target is not an active
    account). The whole apply is rolled back — never a partial write."""


class ApplyProposal(BaseModel):
    """One accepted proposal to persist — the wire shape :func:`as_dicts` emits.

    Exactly one dimension is expressed: ``caller_id`` (execution stage), a
    ``condition`` (complexity / language / artifact), a category (``condition``
    keyed on ``classified_intent`` + ``intent_name`` + ``intent_examples``), or
    ``is_default`` (the workspace default)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    is_default: bool = False
    priority: int = Field(default=10, ge=0)
    caller_id: str | None = Field(default=None, max_length=120)
    condition: ConditionPayload | None = None
    intent_name: str | None = Field(default=None, max_length=120)
    intent_examples: list[str] | None = None

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _shape_valid(self) -> ApplyProposal:
        if self.is_default:
            return self
        if self.intent_name is not None:
            # Category — needs seed examples so the classifier can match. The
            # rule's condition is derived server-side (classified_intent ==
            # intent_name), so a model-supplied condition is not required here.
            if not self.intent_examples:
                raise ValueError("a category proposal requires intent_examples")
            return self
        if not (self.caller_id or self.condition):
            raise ValueError("a non-default proposal must declare a caller_id or a condition")
        return self


async def apply_proposals(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    proposals: list[ApplyProposal],
) -> list[RunRoutingRuleRow]:
    """Persist accepted proposals atomically for ``workspace_id``.

    For each proposal:

    * **category** — create the intent definition (name + seed examples, embedded
      via the account's :class:`EmbeddingService`) THEN the run-routing rule with
      its ``classified_intent`` condition;
    * **caller / condition** — create the run-routing rule with its caller /
      condition;
    * **default** — set the workspace's ``default_account_id`` to the account whose
      ``litellm_model`` == the proposal's ``target``.

    Everything happens in ONE transaction: this flushes each write and commits at
    the end, rolling back on any failure (:class:`ApplyError`). ``account_id`` is
    the personal billing account intents are scoped to (the SAME one the N1
    classifier reads at resolve time). The embedder is resolved once per apply."""
    from backend.embedding.authoring import (  # noqa: PLC0415
        build_account_embedder,
        create_intent_with_examples,
    )
    from backend.identity.workspaces_db import WorkspaceRow  # noqa: PLC0415
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyModelAccountRepository,
        SqlAlchemyRunRoutingRuleRepository,
    )

    accounts_repo = SqlAlchemyModelAccountRepository(session)
    active = await accounts_repo.list_active_for_workspace(workspace_id=workspace_id)
    model_to_account = {a.litellm_model: a for a in active}
    rules_repo = SqlAlchemyRunRoutingRuleRepository(session)

    embedder = None
    if any(p.intent_name is not None and not p.is_default for p in proposals):
        embedder = await build_account_embedder(
            session, workspace_id=workspace_id, account_id=account_id
        )

    try:
        created: list[RunRoutingRuleRow] = []
        for proposal in proposals:
            account = model_to_account.get(proposal.target)
            if account is None:
                raise ApplyError(
                    f"proposal {proposal.name!r} targets {proposal.target!r}, "
                    "which is not an active model account in this workspace"
                )

            if proposal.is_default:
                workspace = await session.get(WorkspaceRow, workspace_id)
                if workspace is None:
                    raise ApplyError("workspace not found")
                workspace.default_account_id = account.id
                await session.flush()
                continue

            conditions: list[dict[str, Any]] = []
            if proposal.intent_name is not None:
                # Category — create the intent def first, then key the rule on it.
                await create_intent_with_examples(
                    session,
                    workspace_id=workspace_id,
                    account_id=account_id,
                    name=proposal.intent_name,
                    threshold=0.65,
                    examples=proposal.intent_examples or [],
                    embedder=embedder,
                )
                conditions.append(
                    {
                        "field": "classified_intent",
                        "operator": "eq",
                        "value": proposal.intent_name,
                        "negate": False,
                    }
                )
            elif proposal.condition is not None:
                conditions.append(proposal.condition.model_dump())

            row = RunRoutingRuleRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                name=proposal.name,
                caller_id=proposal.caller_id,
                priority=proposal.priority,
                is_default=False,
                target=proposal.target,
                conditions=conditions,
                is_active=True,
            )
            await rules_repo.add(row)
            created.append(row)
    except Exception:
        await session.rollback()
        raise

    await session.commit()
    logger.info(
        "run_routing_rules_applied",
        workspace_id=str(workspace_id),
        rules=len(created),
        proposals=len(proposals),
    )
    return created


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[ApplyProposal] = Field(min_length=1)


class ApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: list[RunRuleResponse]
    default_set: bool


@router.post("/compile/apply", status_code=status.HTTP_201_CREATED)
async def apply_compiled_rules(
    payload: ApplyRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApplyResponse:
    """Persist the founder-accepted proposals atomically (create intents + rules,
    set the workspace default). Rolls back on any failure — never partial."""
    try:
        created = await apply_proposals(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            proposals=payload.proposals,
        )
    except ApplyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a rule or intent with one of these names already exists",
        ) from None
    return ApplyResponse(
        created=[_to_response(r) for r in created],
        default_set=any(p.is_default for p in payload.proposals),
    )
