"""Human-readable byte size formatting."""

from __future__ import annotations

__all__ = ["format_bytes"]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(num_bytes: int | float, *, precision: int = 1) -> str:
    """Return *num_bytes* as a human-readable string using binary prefixes.

    Raises ``ValueError`` for negative input.
    """
    if num_bytes < 0:
        raise ValueError(f"num_bytes must be non-negative, got {num_bytes}")

    value = float(num_bytes)
    for unit in _UNITS[:-1]:
        if abs(value) < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.{precision}f} {_UNITS[-1]}"
