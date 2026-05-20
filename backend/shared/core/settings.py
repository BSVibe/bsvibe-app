"""Settings mixins shared by every BSVibe FastAPI service.

The headline contract is the **CSV-list env decoder** — extracted
verbatim from BSupervisor PR #13 §M18. Pydantic-settings 2.x will,
by default, JSON-decode any ``list[str]`` field; if a deployer sets
``CORS_ALLOWED_ORIGINS=http://a.test,http://b.test`` (the legacy
``os.environ.get(...).split(",")`` shape), pydantic raises a
``ValidationError`` because ``http://a.test,...`` is not valid JSON.

The fix is a two-piece pattern that **all four products** must share so
``.env`` files migrate without surprises:

1. Annotate the field as ``Annotated[list[str], NoDecode]`` to opt out
   of pydantic-settings' built-in JSON decode.
2. Attach a ``field_validator(mode="before")`` that uses
   :func:`parse_csv_list` to split the string on commas.

Wire-compatibility: any change to :func:`parse_csv_list`'s splitting/
trimming/empty-token rules is a breaking change for the four products.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BsvibeSettings(BaseSettings):
    """Pydantic-settings base shared by every BSVibe service.

    * ``extra="ignore"`` — products carry their own private settings on
      top of any shared mixin, so unknown env vars must NOT crash startup.
    * ``case_sensitive=False`` — matches the legacy ``os.environ.get``
      behaviour the four products grew up with.
    * ``env_file=None`` — loading ``.env`` is a deployment concern; let
      each product opt in by overriding ``model_config``.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )


def parse_csv_list(value: str | Iterable[str] | None) -> list[str]:
    """Normalise a comma-separated env value into ``list[str]``.

    Behaviour (pinned by tests — wire-compatible with BSupervisor §M18):

    * ``None`` and ``""`` -> ``[]``.
    * ``str`` -> split on ``,``, strip whitespace, drop empty tokens.
    * ``Iterable[str]`` -> strip each entry, drop empty tokens.
    * Anything else -> :class:`TypeError`.
    """

    if value is None:
        return []
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip()]
    if isinstance(value, Iterable):
        return [str(token).strip() for token in value if str(token).strip()]
    raise TypeError(
        f"parse_csv_list expected str | Iterable[str] | None, got {type(value).__name__}"
    )


def csv_list_field(
    *,
    default: list[str] | None = None,
    alias: str | None = None,
    description: str | None = None,
) -> Any:
    """Build a pydantic Field for a CSV-list env var.

    Use together with ``Annotated[list[str], NoDecode]`` and a
    ``field_validator(..., mode="before")`` that calls
    :func:`parse_csv_list`. The helper exists so each product does not
    have to re-derive the ``Field(default_factory=...)`` boilerplate.

    .. code-block:: python

        class Settings(BsvibeSettings):
            cors_allowed_origins: Annotated[list[str], NoDecode] = csv_list_field(
                default=["http://localhost:3500"],
                alias="cors_allowed_origins",
            )

            @field_validator("cors_allowed_origins", mode="before")
            @classmethod
            def _parse_cors(cls, v):
                return parse_csv_list(v) or ["http://localhost:3500"]
    """

    default_value = list(default) if default is not None else []
    kwargs: dict[str, Any] = {"default_factory": lambda: list(default_value)}
    if alias is not None:
        kwargs["validation_alias"] = alias
    if description is not None:
        kwargs["description"] = description
    return Field(**kwargs)


__all__ = [
    "BsvibeSettings",
    "parse_csv_list",
    "csv_list_field",
]
