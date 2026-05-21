"""Tests for backend.supervisor.audit.events — pydantic event wire shape."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.supervisor.audit.events import (
    AuditActor,
    AuditEventBase,
    AuditResource,
)


class _MyEvent(AuditEventBase):
    DEFAULT_EVENT_TYPE = "demo.something.happened"


def _actor() -> AuditActor:
    return AuditActor(type="user", id="user-1", email="u@example.com")


class TestAuditActor:
    def test_minimum_fields(self):
        a = AuditActor(type="service", id="svc-1")
        assert a.type == "service"
        assert a.id == "svc-1"
        assert a.email is None

    def test_rejects_invalid_type(self):
        with pytest.raises(ValidationError):
            AuditActor(type="alien", id="x")  # type: ignore[arg-type]

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            AuditActor(type="user", id="u", surprise="!")  # type: ignore[call-arg]


class TestAuditResource:
    def test_minimum_fields(self):
        r = AuditResource(type="workspace", id="ws-1")
        assert r.type == "workspace"
        assert r.id == "ws-1"


class TestAuditEventBase:
    def test_default_event_type_filled_from_class(self):
        e = _MyEvent(actor=_actor())
        assert e.event_type == "demo.something.happened"

    def test_explicit_event_type_overrides_class_default(self):
        e = _MyEvent(actor=_actor(), event_type="demo.override")
        assert e.event_type == "demo.override"

    def test_event_id_auto_generated_unique(self):
        e1 = _MyEvent(actor=_actor())
        e2 = _MyEvent(actor=_actor())
        assert e1.event_id != e2.event_id

    def test_occurred_at_auto_set(self):
        e = _MyEvent(actor=_actor())
        assert e.occurred_at is not None

    def test_data_payload_defaults_to_empty_dict(self):
        e = _MyEvent(actor=_actor())
        assert e.data == {}

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            _MyEvent(actor=_actor(), surprise=1)  # type: ignore[call-arg]

    def test_resource_optional(self):
        e = _MyEvent(actor=_actor())
        assert e.resource is None

    def test_resource_attached(self):
        r = AuditResource(type="workspace", id="ws-1")
        e = _MyEvent(actor=_actor(), resource=r)
        assert e.resource is not None
        assert e.resource.type == "workspace"

    def test_workspace_and_tenant_optional(self):
        e = _MyEvent(actor=_actor())
        assert e.workspace_id is None
        assert e.tenant_id is None
