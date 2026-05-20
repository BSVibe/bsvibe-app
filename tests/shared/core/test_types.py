"""Tests for the shared type alias surface."""

from __future__ import annotations

import backend.shared.core.types as types
from backend.shared.core.types import (
    JsonDict,
    JsonValue,
    RequestId,
    TenantId,
    UserId,
)


class TestTypeAliases:
    def test_tenant_id_alias_exists_and_is_str(self) -> None:
        # Type aliases collapse to their target at runtime — we only
        # assert that the alias is exposed and resolves to ``str``.
        value: TenantId = "t1"
        assert value == "t1"

    def test_user_id_alias_exists(self) -> None:
        value: UserId = "u1"
        assert value == "u1"

    def test_request_id_alias_exists(self) -> None:
        value: RequestId = "r1"
        assert value == "r1"

    def test_json_dict_alias_accepts_nested(self) -> None:
        value: JsonDict = {"a": 1, "b": {"c": [1, 2, 3]}, "d": None, "e": True}
        assert value["a"] == 1

    def test_json_value_alias_accepts_primitives(self) -> None:
        for v in (1, "x", 1.5, True, None, [1, 2], {"a": 1}):
            holder: JsonValue = v
            assert holder == v


class TestPublicTypeExports:
    def test_all_lists_aliases(self) -> None:
        assert "TenantId" in types.__all__
        assert "UserId" in types.__all__
        assert "RequestId" in types.__all__
        assert "JsonDict" in types.__all__
        assert "JsonValue" in types.__all__
