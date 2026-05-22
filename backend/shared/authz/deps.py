"""FastAPI dependency helpers — authentication only.

Public surface:
- ``get_current_user`` — verify the caller's Supabase session JWT and return
  the authenticated :class:`User`.
- ``CurrentUser`` — ``Annotated[User, Depends(get_current_user)]``.
- ``get_settings_dep`` — override-friendly Settings provider.

Authorization is a separate layer: :func:`backend.api.deps.require_role`
gates by ``Membership.role`` (RBAC). Isolation is the ``workspace_id``
scoping layer. OpenFGA / cross-service service tokens were retired — BSVibe
is a single backend.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security.utils import get_authorization_scheme_param

from .auth import AuthError, parse_user_token, verify_user_jwt
from .settings import Settings, get_settings
from .types import User


def get_settings_dep() -> Settings:
    """Override-friendly Settings provider."""
    return get_settings()


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Authorization scheme",
        )
    return token


async def get_current_user(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    """Resolve the authenticated principal from the bearer session JWT.

    Verifies the Supabase / BSVibe-Auth user JWT (ES256/JWKS in prod, HS256
    in dev) and returns the :class:`User`. Raises 401 on any auth failure.
    """
    token = _extract_bearer(authorization)
    try:
        payload = verify_user_jwt(token, settings)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    return parse_user_token(payload)


CurrentUser = Annotated[User, Depends(get_current_user)]
