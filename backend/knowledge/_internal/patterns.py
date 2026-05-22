"""Shared regex patterns used across knowledge sub-packages."""

from __future__ import annotations

import re

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
