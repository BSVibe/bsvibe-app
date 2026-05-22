"""Role hierarchy — RBAC ordering on Membership.role (Workflow §3)."""

from __future__ import annotations

import pytest

from backend.identity.roles import ROLE_HIERARCHY, role_satisfies


def test_hierarchy_order() -> None:
    assert ROLE_HIERARCHY["owner"] > ROLE_HIERARCHY["admin"]
    assert ROLE_HIERARCHY["admin"] > ROLE_HIERARCHY["editor"]
    assert ROLE_HIERARCHY["editor"] > ROLE_HIERARCHY["viewer"]


@pytest.mark.parametrize(
    ("role", "minimum", "expected"),
    [
        ("owner", "admin", True),
        ("admin", "admin", True),
        ("editor", "admin", False),
        ("viewer", "admin", False),
        ("owner", "owner", True),
        ("admin", "owner", False),
        ("viewer", "viewer", True),
        ("editor", "viewer", True),
    ],
)
def test_role_satisfies(role: str, minimum: str, expected: bool) -> None:
    assert role_satisfies(role, minimum) is expected


def test_unknown_role_never_satisfies() -> None:
    # An unrecognised role string ranks below everything — fail closed.
    assert role_satisfies("banana", "viewer") is False


def test_unknown_minimum_raises() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        role_satisfies("owner", "superadmin")
