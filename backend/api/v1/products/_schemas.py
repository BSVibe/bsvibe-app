"""Shared Pydantic schemas + regex constants for ``/api/v1/products``.

Used by the four endpoint groups (``products_crud`` / ``resources`` /
``bindings`` / ``files``). Pulled into one module so each endpoint file
stays a thin adapter (D35).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# A resource URL, when present, must look like a real http(s) (or mailto) link —
# enough to reject "not a url" without smuggling a strict URL parser in. Empty
# is allowed only via the field being absent/None (see ResourceCreate).
_URL_RE = re.compile(r"^(https?://|mailto:).+", re.IGNORECASE)

_OutputMode = Literal["safe", "direct"]


# --- Products -----------------------------------------------------------------


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
    # Free-form product metadata (no lifecycle enum). When present, REPLACES
    # the stored dict wholesale (no shallow merge) — send the full object you
    # want persisted; omit the key (``None``) to leave it untouched.
    metadata: dict[str, Any] | None = Field(default=None)


class ProductResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    repo_url: str | None = None
    # Lift A v2 — surfaced so the founder UI can render a calm "분석 중…"
    # panel during the background bootstrap. ``None`` on every product
    # created without a ``repo_url`` (bootstrap is skipped entirely).
    bootstrap_status: str | None = None
    bootstrap_artifacts_count: int | None = None
    bootstrap_error: str | None = None
    # Lift E9 — per-chunk progress snapshot while ingest is running:
    # ``{"chunks_done", "chunks_total", "chunks_failed", "notes_created",
    # "notes_updated", "phase"}``. ``None`` outside the ingest window
    # or on every legacy row — founder UI treats ``None`` as "fall back
    # to status pill".
    bootstrap_progress: dict[str, Any] | None = None
    # Free-form product metadata (no lifecycle enum) — always an object, never
    # ``None``. Read from the ORM's ``product_metadata`` attribute (the
    # ``metadata`` attribute name is reserved by SQLAlchemy's declarative base)
    # and surfaced on the wire as ``metadata``.
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="product_metadata")
    created_at: datetime
    updated_at: datetime


class ProductBootstrapResponse(BaseModel):
    """``GET /api/v1/products/{id}/bootstrap`` — progress snapshot.

    Carries the same lifecycle vocabulary the migration documents (see
    :mod:`backend.workflow.application.runtime.product_bootstrap_runtime`
    constants). ``started_at`` / ``completed_at`` are best-effort surfaced
    from the row timestamps — see the repository's ``fetch_progress``.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    status: str | None
    artifacts_count: int | None
    error: str | None
    run_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    # Lift E9 — per-chunk progress snapshot during ingest. ``None``
    # outside the ingest window or before any chunk has finished.
    progress: dict[str, Any] | None = None


# --- Product resources --------------------------------------------------------


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


# --- Resource bindings (per-Product × ConnectorAccount 3-knob binding) -------


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


# --- Product files -----------------------------------------------------------


class FileTreeEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    kind: Literal["file", "dir"]


class ProductFileContentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    truncated: bool = False
    binary: bool = False


__all__ = [
    "FileTreeEntryResponse",
    "ProductBootstrapResponse",
    "ProductCreate",
    "ProductFileContentResponse",
    "ProductResponse",
    "ProductUpdate",
    "ResourceBindingCreate",
    "ResourceBindingResponse",
    "ResourceBindingUpdate",
    "ResourceCreate",
    "ResourceResponse",
    "TriggerKnob",
]
