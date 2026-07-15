"""Structural GUC/scope audit — make the cross-tenant-leak class impossible.

The tenant-isolation tests (``test_tenant_isolation``) spot-check a few routes.
This test makes the guarantee *structural*: it enumerates EVERY REST route in
the real app and asserts that any route depending on a request DB session ALSO
carries the workspace-scoping dependency (``get_workspace_id`` /
``get_current_membership``) that sets the RLS GUC + engages the ORM auto-filter.

A future endpoint that opens a session but forgets to scope it FAILS CI here —
the leak becomes impossible-by-construction, not spot-checked.

Routes that are legitimately workspace-less (health, the OAuth AS, public
webhooks / OAuth callbacks, worker-token routes, deployment-global operator
config, and the membership-scoped ``/workspaces`` surface) are explicitly
allow-listed WITH A REASON below. The test also fails if the allow-list rots
(an entry no longer matching an actually-unscoped DB route).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    get_current_membership,
    get_db_session,
    get_db_session_factory,
    get_workspace_id,
)

# Dependencies that make a route "touch the request DB session".
_DB_DEPS = frozenset({get_db_session, get_db_session_factory})
# Dependencies that set the Postgres RLS GUC + the ORM auto-filter contextvar.
# ``require_role`` / ``require_account_id`` / ``get_output_language`` all depend
# transitively on one of these, so a flattened dependency walk catches them.
_SCOPE_DEPS = frozenset({get_workspace_id, get_current_membership})


# ---------------------------------------------------------------------------
# The allow-list: DB-touching routes that legitimately do NOT scope by GUC.
# Every entry carries the reason it is not a cross-tenant leak. Keyed by
# ``"METHOD /path"``. Reviewed 2026-07-15 (B2).
# ---------------------------------------------------------------------------
WORKSPACELESS_ALLOWLIST: dict[str, str] = {
    # --- Public auth surface: bootstraps identity; no workspace exists yet ---
    "POST /api/auth/login": "login → ensure_user_bootstrapped creates the user + first workspace; pre-workspace",
    "POST /api/auth/oauth/{provider}/callback": "OAuth login/bootstrap; pre-workspace, mounted outside the v1 auth gate",
    # --- Embedded OAuth Authorization Server: global protocol endpoints ------
    "GET /api/oauth/authorize": "embedded OAuth AS authorize; global protocol endpoint, not tenant data",
    "GET /api/oauth/clients/by-client-id/{client_id}": "public DCR client lookup; global registration metadata",
    "POST /api/oauth/introspect": "OAuth AS token introspection; global",
    "POST /api/oauth/register": "anonymous Dynamic Client Registration; global",
    "POST /api/oauth/revoke": "OAuth AS token revoke; scoped by token, global endpoint",
    "POST /api/oauth/token": "OAuth AS token exchange; global",
    # --- Connector OAuth: 3rd-party callbacks + deployment-global operator config
    "GET /api/v1/connectors/oauth/{provider}/callback": "public 3rd-party OAuth callback; resolves workspace from single-use pending state, not the caller",
    "GET /api/v1/connectors/oauth/github/app-manifest/callback": "public GitHub App manifest callback; workspace resolved from single-use pending state",
    "GET /api/v1/connectors/oauth/github/app-status": "reads deployment-global GitHub App provider config (app_credentials — no workspace_id column)",
    "GET /api/v1/connectors/oauth/sentry/install-url": "reads deployment-global Sentry provider config (no workspace_id column)",
    "GET /api/v1/connectors/oauth/sentry/install/callback": "public Sentry install callback; parks a workspace-less UNCLAIMED install (claim-later)",
    "GET /api/v1/connectors/oauth/unclaimed": "lists workspace-less unclaimed installs; by definition not yet bound to any workspace",
    "POST /api/v1/connectors/oauth/{provider}/app-credentials": "operator sets deployment-global provider App creds (app_credentials — no workspace_id column)",
    # --- SSE stream: query-param token auth (EventSource cannot send headers) -
    "GET /api/v1/events/stream": "SSE; query-param token carries + scopes the workspace, mounted outside the v1 auth gate (eventsource-sse-auth-trap)",
    # --- Worker fleet: worker-token / host-OAuth authed, alternate scoping ----
    "POST /api/v1/workers/register": "worker registration via host OAuth credential; workspace resolved from the credential, not a founder session",
    "POST /api/v1/workers/heartbeat": "worker-token authed; scoped by worker identity",
    "POST /api/v1/workers/poll": "worker-token authed; scoped by worker identity",
    "POST /api/v1/workers/result": "worker-token authed; scoped by worker identity",
    # --- Public webhook ingress ----------------------------------------------
    "POST /api/webhooks/{connector}/{webhook_token}": "public webhook ingress; workspace resolved from the per-connector webhook token, not a session",
    # --- Membership-scoped multi-workspace surface (§3) -----------------------
    # The ONE legitimate place scoping is by caller MEMBERSHIP, not the GUC:
    # every row access is gated on an active Membership (get_current_user_row +
    # _owned_workspace → active_for_user_in_workspace); a non-member gets 404.
    "GET /api/v1/workspaces": "lists the caller's OWN workspaces (list_for_user); intentionally multi-workspace, membership-scoped",
    "POST /api/v1/workspaces": "creates a workspace + grants the caller owner membership; pre-scope by definition",
    "GET /api/v1/workspaces/{workspace_id}": "membership-gated read (_owned_workspace → active_for_user_in_workspace); 404 for non-members",
    "PATCH /api/v1/workspaces/{workspace_id}": "membership-gated update (_owned_workspace); 404 for non-members",
    "DELETE /api/v1/workspaces/{workspace_id}": "membership-gated soft-delete (_owned_workspace); 404 for non-members",
}


def _flatten_dependency_calls(dependant: Any) -> set[Any]:
    """All callables in a route's dependency tree (endpoint + sub-deps)."""
    calls: set[Any] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node.call is not None:
            calls.add(node.call)
        stack.extend(node.dependencies)
    return calls


def _route_key(route: APIRoute) -> list[str]:
    methods = sorted(route.methods - {"HEAD", "OPTIONS"})
    return [f"{method} {route.path}" for method in methods]


def _classify(route: APIRoute) -> tuple[bool, bool]:
    """Return ``(touches_db, is_workspace_scoped)`` for a route."""
    calls = _flatten_dependency_calls(route.dependant)
    return bool(calls & _DB_DEPS), bool(calls & _SCOPE_DEPS)


def _unscoped_db_route_keys() -> set[str]:
    """Every ``METHOD /path`` in the real app that touches the DB unscoped."""
    from backend.api.main import create_app

    app = create_app()
    keys: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        touches_db, scoped = _classify(route)
        if touches_db and not scoped:
            keys.update(_route_key(route))
    return keys


def test_every_db_route_is_workspace_scoped_or_explicitly_allowlisted() -> None:
    """The core structural guard.

    Any DB-touching route without the GUC-setting scope dependency must be in
    the reviewed allow-list. A new endpoint that forgets ``get_workspace_id``
    lands here as a CI failure — a candidate cross-tenant leak.
    """
    unscoped = _unscoped_db_route_keys()
    leaks = sorted(unscoped - set(WORKSPACELESS_ALLOWLIST))
    assert not leaks, (
        "DB-touching route(s) without the workspace-scoping dependency "
        "(get_workspace_id / get_current_membership) and NOT in the reviewed "
        "allow-list — each is a candidate cross-tenant leak. Add the scoping "
        "dependency, or allow-list with a reason if legitimately global:\n  " + "\n  ".join(leaks)
    )


def test_allowlist_has_no_stale_entries() -> None:
    """The allow-list cannot rot.

    Every allow-listed key must still correspond to an actually-unscoped
    DB-touching route. A route that GAINS scoping (or is removed) must drop out
    of the allow-list so it never silently masks a future regression.
    """
    unscoped = _unscoped_db_route_keys()
    stale = sorted(set(WORKSPACELESS_ALLOWLIST) - unscoped)
    assert not stale, (
        "allow-list entries no longer match an unscoped DB route (route changed "
        "or was removed) — remove them so the guard stays honest:\n  " + "\n  ".join(stale)
    )


# ---------------------------------------------------------------------------
# Self-check: the classifier actually CATCHES the leak class (RED proof).
# Guards against the guard passing vacuously.
# ---------------------------------------------------------------------------
def test_classifier_flags_a_synthetic_unscoped_db_route() -> None:
    app = FastAPI()

    @app.get("/leak")
    async def _leak(session: Annotated[AsyncSession, Depends(get_db_session)]) -> dict:
        return {}

    @app.get("/safe")
    async def _safe(
        session: Annotated[AsyncSession, Depends(get_db_session)],
        ws: Annotated[object, Depends(get_workspace_id)],
    ) -> dict:
        return {}

    routes = {r.path: r for r in app.routes if isinstance(r, APIRoute)}
    assert _classify(routes["/leak"]) == (True, False)  # flagged: db, unscoped
    assert _classify(routes["/safe"]) == (True, True)  # cleared: db + scoped
