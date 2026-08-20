"""BSVibe shared core library — public API.

What remains is the half production actually calls. The other half (a
``BsvibeSettings`` base, a generic ``HttpClientBase``, an exception hierarchy
and type aliases) was extracted for OTHER products to share and was deleted
2026-08-20: consolidation onto a single product left it with zero consumers.

.. code-block:: python

    from backend.shared.core import (
        configure_logging,
        csv_list_field,
        parse_csv_list,
        redact_url_password,
    )
"""

from __future__ import annotations

from backend.shared.core.http import redact_url_password
from backend.shared.core.logging import configure_logging
from backend.shared.core.settings import csv_list_field, parse_csv_list

__version__ = "0.1.0"

__all__ = [
    "configure_logging",
    "csv_list_field",
    "parse_csv_list",
    "redact_url_password",
]
