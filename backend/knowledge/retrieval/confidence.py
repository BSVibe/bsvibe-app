"""Confidence decay engine — time-based confidence degradation per knowledge layer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class DecayConfig:
    """Halflife values in days per knowledge layer."""

    semantic: int = 365
    episodic: int = 30
    procedural: int = 90
    affective: int = 60

    def halflife_for(self, knowledge_layer: str) -> int:
        """Return the halflife in days for a given knowledge layer."""
        return getattr(self, knowledge_layer, self.semantic)


def decay_factor(days_since_confirmed: float, halflife_days: int) -> float:
    """Calculate the multiplicative decay factor.

    Uses exponential decay: ``0.5 ^ (days / halflife)``.

    Args:
        days_since_confirmed: Days since the confidence was last confirmed.
        halflife_days: The halflife in days (layer-dependent).

    Returns:
        A float in (0.0, 1.0].  Returns 1.0 when *days_since_confirmed* <= 0.
    """
    if days_since_confirmed <= 0 or halflife_days <= 0:
        return 1.0
    return math.pow(0.5, days_since_confirmed / halflife_days)


def effective_confidence(
    base_confidence: float,
    last_confirmed: str | datetime | None,
    knowledge_layer: str = "semantic",
    *,
    config: DecayConfig | None = None,
    now: datetime | None = None,
) -> float:
    """Compute the effective confidence after time-based decay.

    ``effective = base_confidence * decay_factor(days, halflife)``

    Args:
        base_confidence: The recorded confidence (0.0-1.0).
        last_confirmed: ISO date string or datetime of last confirmation.
            If None, no decay is applied (returns base_confidence).
        knowledge_layer: One of semantic/episodic/procedural/affective.
        config: Optional DecayConfig override.
        now: Optional override for current time (for testing).

    Returns:
        The decayed confidence value.
    """
    if last_confirmed is None:
        return base_confidence

    if config is None:
        config = DecayConfig()

    if now is None:
        now = datetime.now(tz=UTC)

    if isinstance(last_confirmed, str):
        try:
            confirmed_dt = datetime.fromisoformat(last_confirmed)
            if confirmed_dt.tzinfo is None:
                confirmed_dt = confirmed_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return base_confidence
    else:
        confirmed_dt = last_confirmed
        if confirmed_dt.tzinfo is None:
            confirmed_dt = confirmed_dt.replace(tzinfo=UTC)

    days = (now - confirmed_dt).total_seconds() / 86400.0
    halflife = config.halflife_for(knowledge_layer)
    factor = decay_factor(days, halflife)
    return base_confidence * factor
