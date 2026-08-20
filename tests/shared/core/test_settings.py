"""Tests for the settings mixin layer.

These tests pin the wire format that 4 BSVibe products (BSGateway,
BSNexus, BSupervisor, BSage) consume after migration. The CSV-list
behaviour is the contract extracted from BSupervisor PR #13 §M18 — any
change here is breaking and forces all four products to re-coordinate.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode

from backend.shared.core.settings import csv_list_field, parse_csv_list


class TestParseCsvList:
    """parse_csv_list normalises the comma-separated env shape.

    BSupervisor M18 contract: ``Annotated[list[str], NoDecode]`` plus a
    ``field_validator(..., mode="before")`` to split on commas. The
    helper centralises that split.
    """

    def test_passthrough_for_list_input(self) -> None:
        assert parse_csv_list(["a", "b"]) == ["a", "b"]

    def test_splits_comma_separated_string(self) -> None:
        assert parse_csv_list("http://a.test,http://b.test") == [
            "http://a.test",
            "http://b.test",
        ]

    def test_strips_whitespace_around_each_token(self) -> None:
        assert parse_csv_list("  a  ,  b  ,c") == ["a", "b", "c"]

    def test_drops_empty_tokens(self) -> None:
        assert parse_csv_list("a,,b,") == ["a", "b"]

    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_csv_list("") == []

    def test_none_returns_empty_list(self) -> None:
        assert parse_csv_list(None) == []

    def test_list_with_whitespace_and_empties(self) -> None:
        assert parse_csv_list(["a ", "", "  ", " b"]) == ["a", "b"]

    def test_rejects_non_string_non_list(self) -> None:
        with pytest.raises(TypeError):
            parse_csv_list(42)  # type: ignore[arg-type]


class TestCsvListField:
    """csv_list_field() is sugar for the M18 NoDecode pattern.

    Products should be able to declare a CSV-list field without
    re-implementing the Annotated + NoDecode + field_validator triplet.
    """

    def test_csv_list_field_default(self) -> None:
        class S(BaseSettings):
            origins: Annotated[list[str], NoDecode] = csv_list_field(
                default=["http://localhost:3500"],
                alias="cors_allowed_origins",
            )

            @field_validator("origins", mode="before")
            @classmethod
            def _parse(cls, v: str | list[str] | None) -> list[str]:
                return parse_csv_list(v) or ["http://localhost:3500"]

        s = S()
        assert s.origins == ["http://localhost:3500"]

    def test_csv_list_field_consumes_alias_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class S(BaseSettings):
            origins: Annotated[list[str], NoDecode] = csv_list_field(
                default=["http://localhost:3500"],
                alias="cors_allowed_origins",
            )

            @field_validator("origins", mode="before")
            @classmethod
            def _parse(cls, v: str | list[str] | None) -> list[str]:
                return parse_csv_list(v) or ["http://localhost:3500"]

        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "a,b")
        s = S()
        assert s.origins == ["a", "b"]

    def test_csv_list_field_records_description(self) -> None:
        class S(BaseSettings):
            origins: Annotated[list[str], NoDecode] = csv_list_field(
                default=[],
                description="Comma-separated allowed origins.",
            )

            @field_validator("origins", mode="before")
            @classmethod
            def _parse(cls, v: str | list[str] | None) -> list[str]:
                return parse_csv_list(v)

        field_info = S.model_fields["origins"]
        assert field_info.description == "Comma-separated allowed origins."
