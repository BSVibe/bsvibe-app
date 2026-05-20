"""CORS middleware helper — single point of CORS truth for the four products.

Wraps Starlette's :class:`CORSMiddleware` with the BSVibe baseline
defaults derived from BSupervisor PR #13 §M18:

* ``allow_origins`` from :attr:`FastApiSettings.cors_allowed_origins`.
* ``allow_credentials=True`` — required by Supabase / shared-cookie SSO
  callers.
* ``allow_methods`` and ``allow_headers`` configurable via the same
  ``Annotated[list[str], NoDecode]`` env-var pattern.

Products migrate from ad-hoc inline ``app.add_middleware(CORSMiddleware,
...)`` calls to:

.. code-block:: python

    from backend.shared.fastapi import add_cors_middleware
    add_cors_middleware(app, settings)
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.shared.fastapi.settings import FastApiSettings


def add_cors_middleware(
    app: FastAPI,
    settings: FastApiSettings,
    *,
    allow_origins: Iterable[str] | None = None,
    allow_methods: Iterable[str] | None = None,
    allow_headers: Iterable[str] | None = None,
    allow_credentials: bool | None = None,
) -> None:
    """Register :class:`CORSMiddleware` on ``app`` using ``settings``.

    Any keyword overrides take precedence over the corresponding
    :class:`FastApiSettings` field — useful for tests and for products
    that compute origins dynamically (e.g. BSGateway falling back to
    ``http://localhost:{api_port}`` when the env var is unset).
    """

    origins = (
        list(allow_origins) if allow_origins is not None else list(settings.cors_allowed_origins)
    )
    methods = (
        list(allow_methods) if allow_methods is not None else list(settings.cors_allow_methods)
    )
    headers = (
        list(allow_headers) if allow_headers is not None else list(settings.cors_allow_headers)
    )
    credentials = (
        settings.cors_allow_credentials if allow_credentials is None else allow_credentials
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=credentials,
        allow_methods=methods,
        allow_headers=headers,
    )


__all__ = ["add_cors_middleware"]
