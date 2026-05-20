"""bsvibe-fastapi — shared FastAPI helpers for BSVibe products.

Stable public surface:

.. code-block:: python

    from backend.shared.fastapi import (
        FastApiSettings,
        add_cors_middleware,
        make_health_router,
        RequestIdMiddleware,
    )

* :class:`FastApiSettings` — pydantic-settings base extending
  :class:`bsvibe_core.BsvibeSettings`. Pins the BSupervisor PR #13 §M18
  ``Annotated[list[str], NoDecode]`` CORS contract.
* :func:`add_cors_middleware` — registers Starlette's
  :class:`CORSMiddleware` with BSVibe baseline defaults.
* :func:`make_health_router` — factory that returns ``/health`` +
  ``/health/deps`` with an injected dependency-check callable.
* :class:`RequestIdMiddleware` — generates / propagates ``x-request-id``
  and binds it into structlog ``contextvars``.

DB pool / SQLAlchemy concerns (e.g. ``pool_pre_ping=True``) live in the
sibling package ``bsvibe-sqlalchemy``; this package is FastAPI-only.
"""

from __future__ import annotations

from backend.shared.fastapi.cors import add_cors_middleware
from backend.shared.fastapi.health import (
    DepsCallable,
    DepsResult,
    make_health_router,
)
from backend.shared.fastapi.middleware import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
)
from backend.shared.fastapi.settings import FastApiSettings

__version__ = "0.2.0"

__all__ = [
    "FastApiSettings",
    "add_cors_middleware",
    "make_health_router",
    "DepsCallable",
    "DepsResult",
    "RequestIdMiddleware",
    "REQUEST_ID_HEADER",
    "__version__",
]
