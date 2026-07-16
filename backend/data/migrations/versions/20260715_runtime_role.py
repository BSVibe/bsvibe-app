"""runtime_role — provision the least-privilege runtime role ``bsvibe_app`` (B2b).

Why this exists
---------------
The app's DB role ``bsvibe`` is a **SUPERUSER with BYPASSRLS**. Postgres RLS —
even with ``FORCE ROW LEVEL SECURITY`` on the six policy tables (installed by
``gdpr_l1_and_rls``) — is therefore **INERT for the app's own connections**:
tenant isolation rests entirely on the app-level ORM auto-filter (layer 2) plus
manual ``WHERE workspace_id`` in raw-SQL paths, with **no DB-level backstop**.

This migration makes RLS a REAL layer-3 backstop by provisioning a NON-superuser
runtime role the app connects as. Run AS THE OWNER (``bsvibe``) — migrations keep
running as the owner; only the app *runtime* DSN points at ``bsvibe_app``.

The role gets EXACTLY the runtime DML privileges the app needs — and nothing
that would defeat the point:

* ``LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`` — so RLS + FORCE
  actually govern it.
* ``USAGE`` on schema ``public``.
* ``SELECT, INSERT, UPDATE, DELETE`` on ALL tables (the runtime does DML across
  effectively every app table; it is deliberately *not* granted DDL —
  ``CREATE / ALTER / DROP / TRUNCATE / REFERENCES / TRIGGER`` — nor ownership).
* ``USAGE, SELECT, UPDATE`` on ALL sequences (serial/identity inserts).
* ``EXECUTE`` on ALL functions (pgvector operators/casts + any app functions;
  extension functions already grant EXECUTE to PUBLIC, this covers user ones).
* ``ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe`` so tables/sequences/functions
  created by FUTURE migrations (owned by ``bsvibe``) are automatically reachable
  by the runtime role — a new table the runtime can't read would be a prod
  outage on the next deploy.

Custom GUCs (``app.current_workspace_id``) need NO grant — a namespaced
parameter (a name containing a dot) is settable via ``set_config`` by any role.

Password / secret
------------------
The password is NOT hardcoded. When ``BSVIBE_APP_DB_PASSWORD`` is present in the
migration process's environment, this migration sets the role's password from it
(bound safely through a transaction-local GUC + ``format(%L)`` so it is never
string-interpolated). Set it in ``deploy/.env.prod`` before the cutover
``alembic upgrade head``; CI sets it in the workflow env. When absent, the role
is still created + granted but keeps whatever password it had (or none) — a
single-role deployment that never connects as ``bsvibe_app`` is unaffected.

Re-runnable: every statement is ``IF NOT EXISTS`` / idempotent, so running it
against the EXISTING prod DB (which a fresh-init script would skip) is safe.

SQLite has no roles/RLS; the whole body is dialect-gated and skipped there so
the unit-test tier is unaffected.

Revision ID: runtime_role
Revises: executor_task_agentic
Create Date: 2026-07-15
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
import structlog
from alembic import op

revision: str = "runtime_role"
down_revision: Union[str, Sequence[str], None] = "executor_task_agentic"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = structlog.get_logger(__name__)

# The least-privilege runtime role the app connects as. A plain identifier
# (validated below) so it can be safely embedded in DDL that cannot bind params.
RUNTIME_ROLE = "bsvibe_app"
_PASSWORD_ENV = "BSVIBE_APP_DB_PASSWORD"  # noqa: S105 — env var NAME, not a secret

# Runtime DML the app needs — NOT DDL. Withholding DDL/ownership/superuser/
# BYPASSRLS is the entire point, so this is least-privilege, not "ALL".
_TABLE_PRIVS = "SELECT, INSERT, UPDATE, DELETE"
_SEQUENCE_PRIVS = "USAGE, SELECT, UPDATE"


def _assert_plain_identifier(name: str) -> str:
    """Guard the role name is a bare identifier (defense-in-depth for the DDL)."""
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ValueError(f"unsafe role identifier: {name!r}")
    return name


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite / others have no roles or RLS.

    role = _assert_plain_identifier(RUNTIME_ROLE)

    # --- Create the role if absent (idempotent) -----------------------------
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            "END IF; END $$;"
        )
    )

    # --- Set its password from the env secret, if provided ------------------
    password = os.environ.get(_PASSWORD_ENV)
    if password:
        # Bind the secret through a transaction-local GUC, then let Postgres
        # quote it with format(%L). No manual escaping, no interpolation.
        bind.execute(
            sa.text("SELECT set_config('bsvibe.provision_app_password', :pw, true)"),
            {"pw": password},
        )
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"EXECUTE format('ALTER ROLE {role} WITH LOGIN PASSWORD %L', "
                "current_setting('bsvibe.provision_app_password')); "
                "END $$;"
            )
        )
    else:
        logger.warning(
            "runtime_role_no_password_set",
            role=role,
            env=_PASSWORD_ENV,
            detail="role created/granted without a password; set the env var to "
            "let it log in over TCP (see docs/e2e/two-role-rls-checklist.md)",
        )

    # --- Grant EXACTLY the runtime privileges -------------------------------
    op.execute(sa.text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    op.execute(sa.text(f"GRANT {_TABLE_PRIVS} ON ALL TABLES IN SCHEMA public TO {role}"))
    op.execute(sa.text(f"GRANT {_SEQUENCE_PRIVS} ON ALL SEQUENCES IN SCHEMA public TO {role}"))
    op.execute(sa.text(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {role}"))

    # --- Default privileges for objects FUTURE migrations create ------------
    # Migrations run as the owner (bsvibe), so future tables/sequences/functions
    # are owned by bsvibe; these ALTER DEFAULT PRIVILEGES ensure the runtime
    # role can reach them without a follow-up grant migration.
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"GRANT {_TABLE_PRIVS} ON TABLES TO {role}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"GRANT {_SEQUENCE_PRIVS} ON SEQUENCES TO {role}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"GRANT EXECUTE ON FUNCTIONS TO {role}"
        )
    )


def downgrade() -> None:
    """Revoke the runtime role's privileges (rollback safety).

    We REVOKE grants + default privileges but deliberately do NOT ``DROP ROLE``:
    the runtime may still hold pooled connections during an operator rollback,
    and the documented rollback is "point the app DSN back at the owner role"
    (see the checklist) — the role can linger harmlessly with no privileges. A
    re-``upgrade`` re-grants idempotently.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    role = _assert_plain_identifier(RUNTIME_ROLE)
    # Only act if the role exists (a fresh DB downgraded to base may not have it).
    exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    if exists is None:
        return

    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"REVOKE {_TABLE_PRIVS} ON TABLES FROM {role}"
        )
    )
    op.execute(
        sa.text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"REVOKE {_SEQUENCE_PRIVS} ON SEQUENCES FROM {role}"
        )
    )
    op.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe IN SCHEMA public "
            f"REVOKE EXECUTE ON FUNCTIONS FROM {role}"
        )
    )
    op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {role}"))
    op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role}"))
    op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}"))
    op.execute(sa.text(f"REVOKE USAGE ON SCHEMA public FROM {role}"))
