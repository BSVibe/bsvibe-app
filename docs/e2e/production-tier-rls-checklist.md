# E2E Checklist — `tests/production` tier + tenant-isolation / GUC audit (B2, INV-3)

Governing spec: `docs/architecture/INVARIANTS.md` **INV-3**. This tier is a
launch blocker: BSVibe is multi-tenant SaaS with public signup, and until now
Postgres RLS + the workspace scoping dependency had **never** been exercised
through the API (66 of 70 API test files override `get_workspace_id` /
`get_db_session` / `get_current_user`, so `set_workspace_guc` never ran).

## How it runs

- Files: `tests/production/{conftest,test_tenant_isolation,test_scope_audit}.py`.
- Collected by the standard CI command (`pytest --cov=backend --cov=plugin
  --cov-fail-under=80`) because `testpaths = ["plugin", "tests"]` — nothing
  excludes the new tier.
- The isolation tests need a real Postgres (RLS is a no-op on SQLite), gated by
  `_support.use_real_pg()` → skip unless `BSVIBE_DATABASE_URL` is set +
  reachable. CI sets it (`pgvector/pgvector:pg16` + `alembic upgrade head`).
- The structural scope-audit test needs **no DB** and runs everywhere.

## The real-JWT crux (no auth override)

- [x] The tier configures the **real** auth path (`backend.shared.authz`) with
  the documented HS256 dev signing secret via `USER_JWT_SECRET` env — a
  *configuration* input, not a `dependency_overrides` entry.
- [x] `mint_jwt()` signs an HS256 token (`sub` / `iat` / `exp`, `aud=bsvibe`)
  that the production `get_current_user` → `verify_user_jwt` validates
  unchanged. Two distinct tenants (`supa-tenant-a`, `supa-tenant-b`).
- [x] `real_app` fixture asserts `app.dependency_overrides == {}` — the tier
  fails loudly if any auth/session/workspace override sneaks in.

## Tenant creation via production paths

- [x] Tenants are created by the **real** `ensure_user_bootstrapped` (the
  function `/api/auth/login` calls) — only the external Supabase GoTrue call is
  skipped (cannot run in CI). No hand-built `*Row` for user/workspace.
- [x] Tenant DATA (products, model accounts) is created by driving real HTTP
  routes with the tenant's JWT.

## Isolation proofs (through the real app)

- [x] **RLS table (`products`)** — Tenant A creates a product; Tenant B's
  `GET /api/v1/products` does not list it, and `GET .../{id}` 404s.
- [x] **App-filter-only table (`model_accounts`, not in the RLS set)** — Tenant
  A creates a model account; Tenant B's `GET /api/v1/accounts` does not list it,
  and `GET .../{id}` 404s. Only the ORM auto-filter / explicit `workspace_id`
  predicate defends this table.
- [x] **GUC fail-open guard** — raw SQL (ORM filter bypassed) proves the RLS
  policy returns EVERY tenant's rows when the GUC is unset, and only tenant A's
  when the GUC = A. Exercised under `SET ROLE` to a non-superuser role (see
  finding below). Documents why `get_workspace_id` (sets the GUC every request)
  is load-bearing.

## Structural scope audit (impossible-by-construction)

- [x] `test_every_db_route_is_workspace_scoped_or_explicitly_allowlisted`
  enumerates every REST route in the real app; any route depending on a request
  DB session (`get_db_session` / `get_db_session_factory`) that lacks the
  scope-setting dependency (`get_workspace_id` / `get_current_membership`, which
  transitively covers `require_role` / `require_account_id` /
  `get_output_language`) must be in the reviewed allow-list — else CI fails.
- [x] `test_allowlist_has_no_stale_entries` — the allow-list cannot rot; an
  entry that no longer matches an unscoped DB route fails.
- [x] `test_classifier_flags_a_synthetic_unscoped_db_route` — RED proof: the
  classifier flags a synthetic unscoped DB route and clears a scoped one, so the
  guard cannot pass vacuously.

## Audit result — routes flagged (all reviewed, zero genuine leaks)

93 DB-touching routes carry the scoping dependency. 26 DB-touching routes are
workspace-less and **all legitimately so** — allow-listed with reasons in
`WORKSPACELESS_ALLOWLIST` (`test_scope_audit.py`): public auth surface, the
embedded OAuth Authorization Server, public 3rd-party OAuth/webhook callbacks,
worker-token routes, deployment-global operator config (`app_credentials` /
unclaimed installs — tables with no `workspace_id`), and the membership-scoped
`/api/v1/workspaces` surface (§3). **No uniform leak fix was needed.**

## Findings for the operators (non-blocking, but important)

- [x] **Superuser bypasses RLS.** The stock `pgvector/pgvector:pg16` image (CI
  AND the Mac Mini prod compose likely) makes the app's `bsvibe` role a
  SUPERUSER with BYPASSRLS. RLS — even with `FORCE ROW LEVEL SECURITY` — is
  therefore **inert for the app's own role**; defense layer 2 (the ORM
  auto-filter) is what actually isolates the API today. For RLS (layer 3) to be
  a real defense, the app must connect as a **non-superuser, non-BYPASSRLS**
  role. Follow-up: provision a least-privilege app role. The fail-open test
  exercises RLS under `SET ROLE` to a governed role so the policy is genuinely
  validated regardless.
- [ ] **Operator-scope routes under the founder auth gate** (observation, not a
  tenant leak): `POST /connectors/oauth/{provider}/app-credentials`,
  `GET .../github/app-status`, `.../sentry/install-url`, `.../unclaimed` write/read
  deployment-global provider config and are reachable by any authenticated
  founder. No cross-tenant DATA leak (tables have no `workspace_id`), but they
  are arguably operator-only — an RBAC follow-up, out of scope for the GUC audit.
