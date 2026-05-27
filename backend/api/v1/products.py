"""/api/v1/products — per-workspace Product CRUD (Workflow §3)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_role
from backend.connectors.db import ConnectorAccountRow
from backend.workspaces.db import ProductResourceRow, ProductRow
from backend.workspaces.resource_bindings import ResourceBindingRepository

logger = structlog.get_logger(__name__)

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# A resource URL, when present, must look like a real http(s) (or mailto) link —
# enough to reject "not a url" without smuggling a strict URL parser in. Empty
# is allowed only via the field being absent/None (see ResourceCreate).
_URL_RE = re.compile(r"^(https?://|mailto:).+", re.IGNORECASE)


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=64)
    repo_url: str | None = Field(default=None, max_length=512)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]*$")
        return v


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    repo_url: str | None = Field(default=None, max_length=512)


class ProductResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    repo_url: str | None = None
    created_at: datetime
    updated_at: datetime


@router.get("")
async def list_products(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ProductResponse]:
    rows = (
        (
            await session.execute(
                select(ProductRow)
                .where(ProductRow.workspace_id == workspace_id)
                .order_by(ProductRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ProductResponse.model_validate(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        slug=payload.slug,
        repo_url=payload.repo_url,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug={payload.slug!r} already exists in this workspace",
        ) from None
    # W1 — every product gets a canonical git workspace immediately.
    # The init is FS-side; a failure here logs but doesn't undo the DB
    # commit (the product is real; the workspace can be re-initialised on
    # next request via the startup hook). The strict failure path (raise +
    # rollback) would force the founder to retry the whole POST on a
    # transient disk error, which is a worse UX.
    from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy to avoid import cycle
        ProductWorkspaceError,
        init_product_workspace,
    )

    try:
        await init_product_workspace(row.id)
    except ProductWorkspaceError:
        logger.warning(
            "product_workspace_init_failed_at_create",
            product_id=str(row.id),
            exc_info=True,
        )
    return ProductResponse.model_validate(row)


@router.get("/{product_id}")
async def get_product(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    return ProductResponse.model_validate(row)


@router.patch("/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    for field in ("name", "repo_url"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await session.commit()
    return ProductResponse.model_validate(row)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_product(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Product resources — named pointers a product works with (repo / doc / deploy
# / note). Workspace-scoped exactly like the parent product: every route first
# resolves the product within the caller's workspace and 404s otherwise, so a
# resource is never reachable across a workspace boundary.
# ---------------------------------------------------------------------------
class ResourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    note: str | None = Field(default=None, max_length=2048)

    @field_validator("kind", "title")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not _URL_RE.match(v):
            raise ValueError("url must be an http(s):// or mailto: link")
        return v


class ResourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: str
    title: str
    url: str | None = None
    note: str | None = None
    created_at: datetime


async def _resolve_product_in_workspace(
    session: AsyncSession, product_id: uuid.UUID, workspace_id: uuid.UUID
) -> ProductRow:
    """Load a product the caller's workspace owns, or raise 404."""
    product = await session.get(ProductRow, product_id)
    if product is None or product.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    return product


@router.get("/{product_id}/resources")
async def list_product_resources(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResourceResponse]:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    rows = (
        (
            await session.execute(
                select(ProductResourceRow)
                .where(ProductResourceRow.product_id == product_id)
                .order_by(ProductResourceRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ResourceResponse.model_validate(r) for r in rows]


@router.post("/{product_id}/resources", status_code=status.HTTP_201_CREATED)
async def add_product_resource(
    product_id: uuid.UUID,
    payload: ResourceCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    row = ProductResourceRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        kind=payload.kind,
        title=payload.title,
        url=payload.url,
        note=payload.note,
    )
    session.add(row)
    await session.commit()
    return ResourceResponse.model_validate(row)


@router.delete(
    "/{product_id}/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_resource(
    product_id: uuid.UUID,
    resource_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    row = await session.get(ProductResourceRow, resource_id)
    if row is None or row.product_id != product_id or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource {resource_id} not found"
        )
    await session.delete(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Resource bindings — per-Product × ConnectorAccount 3-knob binding
# (Workflow §3). Carries ``selection`` / ``trigger`` / ``output_mode``. The
# Receive stage (B10b) resolves an inbound webhook → binding → Product via
# ``ResourceBindingRepository.find_binding``; this surface is the founder's
# CRUD to manage those bindings.
#
# Workspace-scoped exactly like a product resource: every route first resolves
# the product within the caller's workspace and 404s otherwise, and the binding
# repository itself filters every read/write on ``workspace_id``.
# ---------------------------------------------------------------------------
_OutputMode = Literal["safe", "direct"]


class TriggerKnob(BaseModel):
    """The trigger knob — ``{"enabled": bool, "filters": dict}``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    filters: dict[str, Any] = Field(default_factory=dict)


class ResourceBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_account_id: uuid.UUID
    resource_id: str = Field(min_length=1, max_length=512)
    selection: dict[str, Any] = Field(default_factory=dict)
    trigger: TriggerKnob = Field(default_factory=TriggerKnob)
    output_mode: _OutputMode = "safe"


class ResourceBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: dict[str, Any] | None = None
    trigger: TriggerKnob | None = None
    output_mode: _OutputMode | None = None


class ResourceBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID
    connector_account_id: uuid.UUID
    resource_id: str
    selection: dict[str, Any]
    trigger: dict[str, Any]
    output_mode: str
    created_at: datetime
    updated_at: datetime


async def _resolve_connector_account_in_workspace(
    session: AsyncSession,
    connector_account_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> ConnectorAccountRow:
    """Load a connector account the caller's workspace owns, or 404.

    Mirror of :func:`_resolve_product_in_workspace`. Returning 404 (not 400)
    keeps the surface uniform with "this thing isn't here for you".
    """
    account = await session.get(ConnectorAccountRow, connector_account_id)
    if account is None or account.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"connector_account {connector_account_id} not found",
        )
    return account


@router.get("/{product_id}/bindings")
async def list_product_bindings(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResourceBindingResponse]:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    repo = ResourceBindingRepository(session)
    rows = await repo.list_for_product(workspace_id=workspace_id, product_id=product_id)
    return [ResourceBindingResponse.model_validate(r) for r in rows]


@router.post("/{product_id}/bindings", status_code=status.HTTP_201_CREATED)
async def create_product_binding(
    product_id: uuid.UUID,
    payload: ResourceBindingCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceBindingResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    await _resolve_connector_account_in_workspace(
        session, payload.connector_account_id, workspace_id
    )
    repo = ResourceBindingRepository(session)
    row = await repo.create(
        workspace_id=workspace_id,
        product_id=product_id,
        connector_account_id=payload.connector_account_id,
        resource_id=payload.resource_id,
        selection=payload.selection,
        trigger=payload.trigger.model_dump(),
        output_mode=payload.output_mode,
    )
    await session.commit()
    await session.refresh(row)
    return ResourceBindingResponse.model_validate(row)


@router.patch("/{product_id}/bindings/{binding_id}")
async def update_product_binding(
    product_id: uuid.UUID,
    binding_id: uuid.UUID,
    payload: ResourceBindingUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceBindingResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    repo = ResourceBindingRepository(session)
    row = await repo.get(workspace_id=workspace_id, binding_id=binding_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    await repo.update(
        row,
        selection=payload.selection,
        trigger=payload.trigger.model_dump() if payload.trigger is not None else None,
        output_mode=payload.output_mode,
    )
    await session.commit()
    await session.refresh(row)
    return ResourceBindingResponse.model_validate(row)


@router.delete(
    "/{product_id}/bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_binding(
    product_id: uuid.UUID,
    binding_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    # Scope check: the binding must belong to this product (and the repo's get
    # already enforces workspace scope).
    repo = ResourceBindingRepository(session)
    row = await repo.get(workspace_id=workspace_id, binding_id=binding_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    deleted = await repo.delete(workspace_id=workspace_id, binding_id=binding_id)
    if not deleted:
        # Concurrent delete — surface 404 uniformly.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    await session.commit()
