"""Utility module for value clamping."""


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a value to the range [low, high].

    Args:
        value: The value to clamp.
        low: The lower bound of the range (inclusive).
        high: The upper bound of the range (inclusive).

    Returns:
        The value clamped to [low, high].

    Raises:
        ValueError: If low > high.
    """
    if low > high:
        raise ValueError(f"low ({low}) must be <= high ({high})")
    return max(low, min(high, value))
