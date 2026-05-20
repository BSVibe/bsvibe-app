"""FastAPI service settings — extends :class:`bsvibe_core.BsvibeSettings`.

The CORS field shape is the headline contract extracted from
**BSupervisor PR #13 §M18**:

* ``cors_allowed_origins`` is ``Annotated[list[str], NoDecode]`` — opt
  out of pydantic-settings' default JSON decode.
* A ``field_validator(mode="before")`` runs
  :func:`bsvibe_core.parse_csv_list` to split on commas. The legacy
  ``os.environ.get("CORS_ALLOWED_ORIGINS", "...").split(",")`` shape
  used by all four products migrates with **zero** deployer changes.

The companion ``cors_allow_methods`` / ``cors_allow_headers`` fields use
the same NoDecode pattern so deployers can override either via
comma-separated env vars without surprises.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import NoDecode

from backend.shared.core import BsvibeSettings, csv_list_field, parse_csv_list

_DEFAULT_ORIGINS: list[str] = ["http://localhost:3500"]
_DEFAULT_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE"]
# ``X-Active-Tenant`` (Tier 3.2): the raw Supabase JWT carries no tenant
# claim, so the SPA sends the active tenant as this header. It must be in
# the CORS allow-list or the browser preflight fails before the request.
_DEFAULT_HEADERS: list[str] = ["Authorization", "Content-Type", "X-Active-Tenant"]


class FastApiSettings(BsvibeSettings):
    """Settings every BSVibe FastAPI service consumes.

    Products extend this class and add their own private fields. The
    inherited ``model_config`` keeps ``extra="ignore"`` so coexistence
    with private settings does not crash startup.
    """

    cors_allowed_origins: Annotated[list[str], NoDecode] = csv_list_field(
        default=_DEFAULT_ORIGINS,
        alias="cors_allowed_origins",
        description="Comma-separated list of CORS allow_origins entries.",
    )

    cors_allow_methods: Annotated[list[str], NoDecode] = csv_list_field(
        default=_DEFAULT_METHODS,
        alias="cors_allow_methods",
        description="Comma-separated list of HTTP methods allowed by CORS.",
    )

    cors_allow_headers: Annotated[list[str], NoDecode] = csv_list_field(
        default=_DEFAULT_HEADERS,
        alias="cors_allow_headers",
        description="Comma-separated list of request headers allowed by CORS.",
    )

    cors_allow_credentials: bool = True

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: str | list[str] | None) -> list[str]:
        return parse_csv_list(value) or list(_DEFAULT_ORIGINS)

    @field_validator("cors_allow_methods", mode="before")
    @classmethod
    def _parse_methods(cls, value: str | list[str] | None) -> list[str]:
        return parse_csv_list(value) or list(_DEFAULT_METHODS)

    @field_validator("cors_allow_headers", mode="before")
    @classmethod
    def _parse_headers(cls, value: str | list[str] | None) -> list[str]:
        return parse_csv_list(value) or list(_DEFAULT_HEADERS)


__all__ = ["FastApiSettings"]
