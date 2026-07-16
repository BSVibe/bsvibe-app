# Two-role Postgres — RLS as a REAL layer-3 backstop (B2b)

**INV-3 / tenant isolation on a public-signup SaaS.** Before this change the app's
DB role `bsvibe` was a **SUPERUSER with BYPASSRLS**, so Postgres RLS — even with
`ENABLE` + `FORCE ROW LEVEL SECURITY` on the six policy tables — was **INERT for
the app's own connections**. Isolation rested entirely on the app-level ORM
auto-filter (layer 2) + manual `WHERE workspace_id` in raw SQL, with **no
DB-level backstop**. This change splits the single role into two so RLS actually
bites.

| Role | Attributes | Used by | RLS governs it? |
|---|---|---|---|
| `bsvibe` (OWNER) | SUPERUSER / BYPASSRLS | **alembic migrations only** (`BSVIBE_MIGRATION_DATABASE_URL`) | No (by design — owns + migrates) |
| `bsvibe_app` (RUNTIME) | `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE` | **backend API + worker** (`BSVIBE_DATABASE_URL`) | **Yes — layer-3 active** |

## What the runtime role is granted (the full set)

Provisioned idempotently by migration `runtime_role`
(`backend/data/migrations/versions/20260715_runtime_role.py`), run AS THE OWNER:

- `USAGE` on schema `public`
- `SELECT, INSERT, UPDATE, DELETE` on **ALL tables** (runtime DML — deliberately
  **no** DDL: `CREATE / ALTER / DROP / TRUNCATE / REFERENCES / TRIGGER`, no
  ownership, no superuser, no BYPASSRLS)
- `USAGE, SELECT, UPDATE` on **ALL sequences**
- `EXECUTE` on **ALL functions** (pgvector operators/casts use extension
  functions that already `EXECUTE` to `PUBLIC`; this covers app functions)
- `ALTER DEFAULT PRIVILEGES FOR ROLE bsvibe` granting the same on TABLES /
  SEQUENCES / FUNCTIONS created by **FUTURE** migrations — so a new table the
  runtime can't read never becomes a next-deploy outage
- Custom GUC `app.current_workspace_id` needs **no grant** (a namespaced
  parameter is settable via `set_config` by any role) — the RLS policy reads it.

**Completeness was proven empirically**, not guessed: the whole test suite
(5067 tests, 89% coverage) runs with the app engine connected as `bsvibe_app`.
A missing grant would surface as `permission denied` at runtime; none did.

## The two layers, kept explicit (`tests/production/test_tenant_isolation.py`)

- **Layer 3 (RLS)** — `test_rls_is_active_layer3_for_the_runtime_role`: asserts
  `current_user` is NOT super / NOT BYPASSRLS, then with RAW SQL on the actual
  `bsvibe_app` connection shows fail-open on unset GUC and **isolation** when the
  GUC = tenant A. No `SET ROLE` hack (the old test needed one because the app
  role bypassed RLS).
- **Layer 2 (ORM filter)** — `test_app_filter_only_table_model_accounts_isolated`:
  `model_accounts` has no RLS policy, so the ORM auto-filter is the only defence.

Data-tier `tests/data/test_rls_pg.py` still mints its own non-super role and
proves the policy independently.

---

## Prod cutover runbook (Mac-Mini prod DB — EXISTING data)

A fresh-init script (`/docker-entrypoint-initdb.d`) does **not** fire on a DB
that already has data, so the role is provisioned by the idempotent `runtime_role`
alembic migration instead. Run these on the prod host after merge.

### 1. Choose + record the runtime password

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Edit `deploy/.env.prod`

```bash
# NEW — runtime role password (assigned to bsvibe_app by the migration)
BSVIBE_APP_DB_PASSWORD=<the-generated-password>

# CHANGED — the app + worker now connect as the runtime role
BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe_app:<the-generated-password>@postgres:5432/bsvibe

# NEW — alembic connects as the OWNER (unchanged bsvibe superuser DSN)
BSVIBE_MIGRATION_DATABASE_URL=postgresql+asyncpg://bsvibe:<BSVIBE_DB_PASSWORD>@postgres:5432/bsvibe
```

`BSVIBE_DB_PASSWORD` (the owner password that seeded the container) is unchanged.

### 3. Deploy — the backend entrypoint provisions the role

The backend service migrates on boot: `alembic upgrade head` runs as the OWNER
(`BSVIBE_MIGRATION_DATABASE_URL`) and the `runtime_role` migration `CREATE`s
`bsvibe_app`, sets its password from `BSVIBE_APP_DB_PASSWORD`, and grants it the
full runtime privilege set + default privileges. The API + worker then connect
as `bsvibe_app` (`BSVIBE_DATABASE_URL`).

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d
```

> If you prefer to provision BEFORE the app rolls, run the migration by hand
> first (owner DSN + app password in the env), then `up -d`:
> ```bash
> BSVIBE_MIGRATION_DATABASE_URL=postgresql+asyncpg://bsvibe:<owner-pw>@localhost:<port>/bsvibe \
> BSVIBE_APP_DB_PASSWORD=<app-pw> \
>   docker compose ... exec backend uv run alembic upgrade head
> ```

### 4. Verify RLS is ACTIVE (not bypassed)

```bash
# The role exists and is NON-super / NON-bypassrls:
docker compose ... exec postgres \
  psql -U bsvibe -d bsvibe -c \
  "SELECT rolname,rolsuper,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname='bsvibe_app';"
#  bsvibe_app | f | f | t   ← required

# The app is actually connecting as it:
docker compose ... exec postgres \
  psql -U bsvibe -d bsvibe -c \
  "SELECT usename, count(*) FROM pg_stat_activity WHERE datname='bsvibe' GROUP BY usename;"
#  bsvibe_app should appear (the backend + worker connections)

# Fail-open unset vs isolated when scoped, AS bsvibe_app (RLS bites):
docker compose ... exec postgres bash -lc \
  "PGPASSWORD=<app-pw> psql -h 127.0.0.1 -U bsvibe_app -d bsvibe -c \
   \"SELECT set_config('app.current_workspace_id','<some-real-ws-uuid>',false); \
     SELECT count(DISTINCT workspace_id) FROM products;\""
#  → 1 (only the scoped workspace). With the GUC unset it returns all (fail-open).
```

Then smoke-test the live app: log in as two different founders and confirm each
sees only their own products/runs (the app path already sets the GUC per
request via `get_workspace_id`).

### Rollback (reversible)

Point the app DSN back at the owner role and redeploy — no migration downgrade
needed, and no data touched:

```bash
# deploy/.env.prod
BSVIBE_DATABASE_URL=postgresql+asyncpg://bsvibe:<BSVIBE_DB_PASSWORD>@postgres:5432/bsvibe
```
```bash
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d
```

The app is a superuser again (RLS inert, back to layer-2-only) — the exact
pre-change posture. The `bsvibe_app` role can linger harmlessly. To also revoke
its privileges, `alembic downgrade runtime_role` (owner DSN) revokes the grants
but intentionally keeps the role (it may hold pooled connections). Re-`upgrade`
re-grants idempotently.

## CI

`.github/workflows/ci.yml` sets `BSVIBE_DATABASE_URL` = `bsvibe_app` (runtime),
`BSVIBE_MIGRATION_DATABASE_URL` = `bsvibe` (owner), and `BSVIBE_APP_DB_PASSWORD`.
The `alembic upgrade head` step (owner) provisions the role BEFORE `pytest`, and
the whole suite — including the production-tier RLS test — runs with the app on
the non-superuser role, so it proves layer-3 isolation rather than asserting it.
