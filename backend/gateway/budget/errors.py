"""Budget enforcement errors."""

from __future__ import annotations


class BudgetExceeded(Exception):
    """Projected cost would push an account over its budget cap."""

    def __init__(self, *, scope: str, current_cents: int, cap_cents: int) -> None:
        super().__init__(
            f"budget exceeded for scope={scope} current={current_cents}c cap={cap_cents}c"
        )
        self.scope = scope
        self.current_cents = current_cents
        self.cap_cents = cap_cents
