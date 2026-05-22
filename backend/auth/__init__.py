"""Supabase IdP wrapper (Workflow §2.1 / §2.2 ``backend/auth/``).

BSVibe calls Supabase GoTrue directly — there is no in-house auth-server.
This package holds the thin async client used by the ``/api/auth/*`` routes
for login, OAuth code exchange, refresh and logout. JWT *verification* of the
resulting access tokens is handled by :mod:`backend.shared.authz`.
"""
