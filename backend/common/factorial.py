"""Utility module for factorial computation."""


def factorial(n: int) -> int:
    """Return n! (n factorial).

    Args:
        n: Non-negative integer.

    Returns:
        n! where factorial(0) == 1.

    Raises:
        ValueError: If n is negative.
    """
    if n < 0:
        raise ValueError(f"factorial is not defined for negative integers (got {n})")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result
