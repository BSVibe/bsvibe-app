"""Text case conversion utilities."""

import re


def to_kebab(value: str) -> str:
    """Convert *value* to kebab-case.

    Lowercases the input, replaces every run of non-alphanumeric characters
    with a single hyphen, and strips leading/trailing hyphens.  Returns an
    empty string for empty or all-symbol input.
    """
    lowered = value.lower()
    kebab = re.sub(r"[^a-z0-9]+", "-", lowered)
    return kebab.strip("-")
