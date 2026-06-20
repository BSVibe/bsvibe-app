"""Human-readable byte-size formatter using binary (IEC) units."""

from __future__ import annotations

__all__: list[str] = ["format_bytes"]

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_STEP = 1024


def format_bytes(num_bytes: int, *, precision: int = 1) -> str:
    """Return *num_bytes* as a human-readable string with binary units.

    Examples::

        >>> format_bytes(0)
        '0 B'
        >>> format_bytes(1536)
        '1.5 KiB'
        >>> format_bytes(1048576)
        '1.0 MiB'

    Args:
        num_bytes: Non-negative byte count to format.
        precision: Number of decimal places in the formatted value.

    Raises:
        ValueError: If *num_bytes* is negative.
    """
    if num_bytes < 0:
        raise ValueError(f"num_bytes must be non-negative, got {num_bytes}")

    value: float = float(num_bytes)
    for unit in _UNITS[:-1]:
        if value < _STEP:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.{precision}f} {unit}"
        value /= _STEP

    return f"{value:.{precision}f} {_UNITS[-1]}"
