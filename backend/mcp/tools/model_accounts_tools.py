"""Model account tools — UI-parity setup surface (Lift D3a).

Wraps :class:`backend.router.accounts.service.ModelAccountService` so the
founder can register, list, show, and delete LLM provider credentials
through the MCP wire — the same surface the PWA's Settings → Models tab
talks to via ``/api/v1/accounts``. Handlers are thin: validate input →
call the existing service → serialize the redacted ``ModelAccountOut``.

The plaintext ``api_key`` flows in only on create (the service encrypts
it at the boundary, the row column is the ciphertext); list / show
responses NEVER echo it back — :class:`ModelAccountOut` exposes only a
``has_api_key`` boolean. There is no update tool in v1; delete-and-recreate
covers the small rotation case without a second sensitive endpoint.

Scopes match the existing convention: ``mcp:read`` for list / show,
``mcp:write`` for create / delete (the same scope :mod:`safe_mode_tools`
uses for irreversible mutations).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.account_service import ensure_personal_account
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.schemas import ModelAccountCreate, ModelAccountOut
from backend.router.accounts.service import ModelAccountService


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


def _service(ctx: ToolContext) -> ModelAccountService:
    return ModelAccountService(ctx.session, cipher=CredentialCipher(_key_from_settings()))


async def _resolve_account_id(ctx: ToolContext) -> uuid.UUID:
    """Resolve the workspace's personal billing account.

    Mirrors the REST :func:`backend.api.deps.require_account_id` get-or-create
    semantics — MCP does not carry the ``X-BSVibe-Account-Id`` header axis
    today, so the personal account is the canonical entry point.
    """
    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    return account.id


def _out_to_dict(row: ModelAccountOut) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "account_id": str(row.account_id),
        "provider": row.provider,
        "label": row.label,
        "litellm_model": row.litellm_model,
        "api_base": row.api_base,
        "data_jurisdiction": row.data_jurisdiction,
        "is_active": row.is_active,
        "has_api_key": row.has_api_key,
        "extra_params": row.extra_params,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# bsvibe_model_accounts_list
# ---------------------------------------------------------------------------
class ModelAccountsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    only_active: bool = False


async def _h_list(args: ModelAccountsListInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    rows = await _service(ctx).list_(
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
        only_active=args.only_active,
    )
    return _Envelope([_out_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_model_accounts_show
# ---------------------------------------------------------------------------
class ModelAccountsShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_account_id: uuid.UUID


async def _h_show(args: ModelAccountsShowInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    row = await _service(ctx).get(
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
        model_account_id=args.model_account_id,
    )
    if row is None:
        raise ToolError(f"model account not found: {args.model_account_id}")
    return _Envelope(_out_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_model_accounts_create
# ---------------------------------------------------------------------------
class ModelAccountsCreateInput(BaseModel):
    """Inbound — plaintext ``api_key`` is encrypted at the service boundary.

    The wire shape mirrors :class:`ModelAccountCreate` (the REST schema). We
    accept the same fields the PWA's Add-Model form posts so MCP and PWA land
    on one validation chain.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=128)
    litellm_model: str = Field(..., min_length=1, max_length=255)
    api_key: str = Field(..., min_length=1)
    api_base: str | None = None
    data_jurisdiction: str = "unknown"
    extra_params: dict[str, Any] = Field(default_factory=dict)


async def _h_create(args: ModelAccountsCreateInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    payload = ModelAccountCreate.model_validate(args.model_dump())
    created = await _service(ctx).create(
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
        payload=payload,
    )
    await ctx.session.commit()
    return _Envelope(_out_to_dict(created))


# ---------------------------------------------------------------------------
# bsvibe_model_accounts_delete
# ---------------------------------------------------------------------------
class ModelAccountsDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_account_id: uuid.UUID


class ModelAccountsDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool
    model_account_id: str


async def _h_delete(args: ModelAccountsDeleteInput, ctx: ToolContext) -> Any:
    account_id = await _resolve_account_id(ctx)
    deleted = await _service(ctx).delete(
        workspace_id=ctx.principal.workspace_id,
        account_id=account_id,
        model_account_id=args.model_account_id,
    )
    if not deleted:
        raise ToolError(f"model account not found: {args.model_account_id}")
    await ctx.session.commit()
    return ModelAccountsDeleteOutput(deleted=True, model_account_id=str(args.model_account_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_model_accounts_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_model_accounts_list",
            description=(
                "List ModelAccounts (LLM provider credentials) in the active "
                "workspace. Responses are redacted — the api_key is never shown, "
                "only a `has_api_key` flag."
            ),
            input_schema=ModelAccountsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_model_accounts_show",
            description="Show one ModelAccount by id. Redacted — no api_key in response.",
            input_schema=ModelAccountsShowInput,
            output_schema=_Envelope,
            handler=_h_show,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_model_accounts_create",
            description=(
                "Register a new ModelAccount for the active workspace. The "
                "plaintext `api_key` is encrypted at the service boundary; the "
                "response carries only the redacted view."
            ),
            input_schema=ModelAccountsCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.model_accounts_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_model_accounts_delete",
            description=(
                "Permanently delete a ModelAccount by id. Use delete-and-recreate "
                "for credential rotation (there is no update tool in v1)."
            ),
            input_schema=ModelAccountsDeleteInput,
            output_schema=ModelAccountsDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.model_accounts_delete.invoked",
        )
    )


__all__ = ["register_model_accounts_tools"]
