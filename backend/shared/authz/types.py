"""Pydantic models exposed to package consumers.

BSVibe is a single backend: authorization is RBAC on ``Membership.role``
(see :mod:`backend.identity.roles`), not cross-service ReBAC. This module
carries only the authenticated principal — the cross-service service-token /
tenant / permission types were retired with OpenFGA.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class User(BaseModel):
    """Authenticated principal — the verified Supabase identity.

    The tenant a request operates within is no longer carried on the token;
    it is resolved at the request layer from the caller's ``Membership`` →
    ``workspace_id`` (Workflow §3). ``is_service`` distinguishes a machine
    principal for audit attribution.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    email: str | None = None
    is_service: bool = False
