"""/api/v1/products — per-workspace Product CRUD (Workflow §3)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_role
from backend.workspaces.db import ProductResourceRow, ProductRow

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
