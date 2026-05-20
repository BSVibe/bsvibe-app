"""Common exception hierarchy shared across the BSVibe ecosystem.

Every product-specific exception SHOULD inherit from :class:`BsvibeError`
so that callers (FastAPI handlers, audit relays) can catch a single base
type. Errors carry a ``context`` dict so structured logs can attach
``tenant_id`` / ``request_id`` without losing the original message.
"""

from __future__ import annotations

from typing import Any


class BsvibeError(Exception):
    """Base class for every BSVibe runtime error.

    Parameters
    ----------
    message:
        Human-readable error message.
    context:
        Optional structured context — typically ``tenant_id``,
        ``request_id``, or any identifier that helps trace the failure.
    """

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context) if context else {}

    def __repr__(self) -> str:
        ctx = f", context={self.context}" if self.context else ""
        return f"{type(self).__name__}({self.message!r}{ctx})"


class ConfigurationError(BsvibeError):
    """Raised when settings/configuration are invalid at startup."""


class ValidationError(BsvibeError):
    """Raised when an external input fails domain validation.

    Distinct from :class:`pydantic.ValidationError` — that one belongs
    to the schema layer; this one is for business-rule violations.
    """


class NotFoundError(BsvibeError):
    """Raised when a requested resource does not exist for the caller."""
