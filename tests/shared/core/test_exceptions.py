"""Tests for the BsvibeError base + standard subclasses."""

from __future__ import annotations

import pytest

from backend.shared.core.exceptions import (
    BsvibeError,
    ConfigurationError,
    NotFoundError,
    ValidationError,
)


class TestBsvibeError:
    def test_is_an_exception(self) -> None:
        assert issubclass(BsvibeError, Exception)

    def test_default_message(self) -> None:
        err = BsvibeError("boom")
        assert str(err) == "boom"

    def test_carries_context(self) -> None:
        err = BsvibeError("boom", context={"tenant_id": "t1", "request_id": "r1"})
        assert err.context == {"tenant_id": "t1", "request_id": "r1"}

    def test_default_context_is_empty_dict(self) -> None:
        err = BsvibeError("boom")
        assert err.context == {}

    def test_can_be_raised(self) -> None:
        with pytest.raises(BsvibeError):
            raise BsvibeError("nope")

    def test_subclasses_inherit_context(self) -> None:
        err = NotFoundError("project not found", context={"project_id": "p1"})
        assert err.context == {"project_id": "p1"}
        assert isinstance(err, BsvibeError)

    def test_repr_includes_class_and_message(self) -> None:
        err = BsvibeError("oops", context={"k": "v"})
        text = repr(err)
        assert "BsvibeError" in text
        assert "oops" in text


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "exc_cls",
        [ConfigurationError, ValidationError, NotFoundError],
    )
    def test_subclasses_inherit_from_bsvibe_error(
        self,
        exc_cls: type[BsvibeError],
    ) -> None:
        assert issubclass(exc_cls, BsvibeError)

    def test_hierarchy_is_distinct(self) -> None:
        # No two siblings should be parent/child of each other.
        assert not issubclass(NotFoundError, ValidationError)
        assert not issubclass(ValidationError, NotFoundError)
        assert not issubclass(ConfigurationError, ValidationError)
