"""types.py — the authenticated principal."""

from __future__ import annotations


def test_user_minimal() -> None:
    from backend.shared.authz.types import User

    u = User(id="00000000-0000-0000-0000-000000000001", email="alice@bsvibe.dev")
    assert u.id == "00000000-0000-0000-0000-000000000001"
    assert u.email == "alice@bsvibe.dev"
    assert u.is_service is False


def test_user_email_optional() -> None:
    from backend.shared.authz.types import User

    u = User(id="alice")
    assert u.email is None


def test_service_user_marker() -> None:
    from backend.shared.authz.types import User

    u = User(id="service:worker", is_service=True)
    assert u.is_service is True


def test_user_ignores_extra_fields() -> None:
    # extra="ignore" — stale claim keys from a richer token don't break parsing.
    from backend.shared.authz.types import User

    u = User.model_validate({"id": "alice", "active_tenant_id": "t-1", "scope": ["*"]})
    assert u.id == "alice"
    assert not hasattr(u, "active_tenant_id")
