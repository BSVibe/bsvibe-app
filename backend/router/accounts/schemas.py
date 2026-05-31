"""Pydantic in/out schemas for ModelAccount."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Allow-list matches backend.plugins.VALID_JURISDICTIONS — single source
# of truth lives in workspace + plugin spec; ModelAccount mirrors the same
# values so the validator chain is uniform.
Jurisdiction = Literal["us", "eu", "kr", "local", "unknown"]


class ModelAccountCreate(BaseModel):
    """Inbound — caller supplies plaintext api_key; service encrypts it."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    litellm_model: str = Field(min_length=1, max_length=255)
    api_base: str | None = None
    api_key: str = Field(min_length=1)
    # Invisible-infra: the founder no longer hand-picks this. Optional in the
    # request body; defaults to "unknown" so the NOT NULL column is always
    # populated. Explicit callers (worker SDK, tests) may still supply a value.
    data_jurisdiction: Jurisdiction = "unknown"
    extra_params: dict[str, Any] = Field(default_factory=dict)


class ModelAccountUpdate(BaseModel):
    """Partial update — every field optional."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=128)
    litellm_model: str | None = Field(default=None, min_length=1, max_length=255)
    api_base: str | None = None
    api_key: str | None = Field(default=None, min_length=1)
    data_jurisdiction: Jurisdiction | None = None
    is_active: bool | None = None
    extra_params: dict[str, Any] | None = None


class ModelAccountOut(BaseModel):
    """Outbound — never exposes encrypted api_key bytes, only a flag."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    provider: str
    label: str
    litellm_model: str
    api_base: str | None
    data_jurisdiction: Jurisdiction
    is_active: bool
    has_api_key: bool
    extra_params: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: Any) -> ModelAccountOut:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            account_id=row.account_id,
            provider=row.provider,
            label=row.label,
            litellm_model=row.litellm_model,
            api_base=row.api_base,
            data_jurisdiction=row.data_jurisdiction,
            is_active=row.is_active,
            has_api_key=bool(row.api_key_encrypted),
            extra_params=row.extra_params,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
