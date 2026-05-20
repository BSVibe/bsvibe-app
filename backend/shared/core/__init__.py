"""BSVibe shared core library — public API.

Stable imports for product code:

.. code-block:: python

    from backend.shared.core import (
        BsvibeSettings,
        configure_logging,
        BsvibeError,
        ConfigurationError,
        ValidationError,
        NotFoundError,
        csv_list_field,
        parse_csv_list,
    )
    from backend.shared.core.types import TenantId, UserId, RequestId, JsonDict, JsonValue
"""

from __future__ import annotations

from backend.shared.core.exceptions import (
    BsvibeError,
    ConfigurationError,
    NotFoundError,
    ValidationError,
)
from backend.shared.core.http import HttpClientBase, redact_headers
from backend.shared.core.logging import configure_logging
from backend.shared.core.settings import (
    BsvibeSettings,
    csv_list_field,
    parse_csv_list,
)

__version__ = "0.1.0"

__all__ = [
    "BsvibeSettings",
    "configure_logging",
    "BsvibeError",
    "ConfigurationError",
    "ValidationError",
    "NotFoundError",
    "HttpClientBase",
    "redact_headers",
    "csv_list_field",
    "parse_csv_list",
    "__version__",
]
