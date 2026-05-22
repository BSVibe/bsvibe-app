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
from backend.workspaces.db import ProductRow

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


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
