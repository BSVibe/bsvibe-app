# BSVibe — Deployment Runbook

Production deploy for **this** monorepo: one FastAPI backend + a worker daemon
+ Postgres (pgvector) + Redis, as Docker containers. The PWA is deployed
separately on Vercel (see [§6](#6-pwa-vercel)).

Files in this directory:

| File | Purpose |
| --- | --- |
| `compose.yaml` | Base stack (dev defaults: PG `bsvibe/bsvibe`, env=dev, ports published). |
| `compose.prod.yaml` | Prod override — env-driven config, no public PG/Redis ports, persistent volume. |
| `Dockerfile.backend` | Backend + worker image. Entrypoint runs `alembic upgrade head` then the API. |
| `Dockerfile.pwa` | Optional containerized PWA (Vercel is the primary path). |
| `Dockerfile.sandbox` | LLM execution sandbox image (built only for the worker/LLM phase). |
| `entrypoint.sh` | Migrate-on-boot wrapper for the backend (api) service. |
| `.env.prod.example` | Every required prod env var, documented. Copy → `.env.prod`. |

---

## How migrations run (and why the worker never races)

The backend image's `ENTRYPOINT` is `deploy/entrypoint.sh`, which runs
`uv run alembic upgrade head` **once** and then `exec`s the CMD (uvicorn).
`alembic.ini` lives at the repo root and is COPYed into the image at `/app`;
the migration scripts ship under `backend/data/migrations/` (already in the
`backend/` copy). Alembic is idempotent — on a restart it sees
`alembic_version` already at head and no-ops.

**Only the `backend` (api) service migrates.** The `worker` service overrides
the entrypoint (`entrypoint: []` in `compose.yaml`) and starts directly into
`python -m backend.workers`, so the two containers never run `alembic upgrade
head` concurrently.

---

## 1. Prerequisites

- Docker + Docker Compose v2.24+ (the prod override uses the `!reset` tag).
- A host (VM / Mac Mini) reachable on the chosen public ports.
- Supabase project (URL + anon key) for auth.

## 2. Generate secrets

**Credential encryption key** (`BSVIBE_GATEWAY_KMS_KEY_B64`) — AES-256-GCM,
32-byte key in **URL-safe** base64. `backend/router/accounts/crypto.py` does
`base64.urlsafe_b64decode(...)` and asserts exactly 32 decoded bytes:

```sh
python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

> Do NOT use `openssl rand -base64 32` — that emits the **standard** base64
> alphabet (may contain `+` or `/`), which `urlsafe_b64decode` rejects, and the
> first credential write would raise at runtime.

**DB password** — any strong random string:

```sh
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

## 3. Fill `.env.prod`

```sh
cp deploy/.env.prod.example deploy/.env.prod
$EDITOR deploy/.env.prod
```

Required (no defaults — compose refuses to start without them):

- `BSVIBE_DB_PASSWORD` — the OWNER role (`bsvibe` superuser) password; seeds the
  postgres container and must match `BSVIBE_MIGRATION_DATABASE_URL`.
- `BSVIBE_APP_DB_PASSWORD` — the RUNTIME role (`bsvibe_app`) password; the
  `runtime_role` migration assigns it and it must match `BSVIBE_DATABASE_URL`.
- `BSVIBE_DATABASE_URL` — RUNTIME DSN the app + worker connect with:
  `postgresql+asyncpg://bsvibe_app:<app-password>@postgres:5432/bsvibe`. This is a
  **NON-superuser** role so Postgres RLS is a real layer-3 tenant-isolation
  backstop (B2b). See `docs/e2e/two-role-rls-checklist.md`.
- `BSVIBE_MIGRATION_DATABASE_URL` — OWNER DSN alembic runs as (DDL + role/policy
  management): `postgresql+asyncpg://bsvibe:<db-password>@postgres:5432/bsvibe`.
  (host is the compose service name `postgres`).
- `BSVIBE_GATEWAY_KMS_KEY_B64` — from step 2.
- `BSVIBE_SUPABASE_URL`, `BSVIBE_SUPABASE_PUBLISHABLE_KEY` — real Supabase project
  (the **publishable** key, `sb_publishable_...`).

`deploy/.env.prod` is gitignored. **Never commit it.**

## 4. Build images (with GIT_SHA)

```sh
# From the repo root.
export GIT_SHA=$(git rev-parse --short HEAD)
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod build
```

`GIT_SHA` is baked into the image (surfaced at `/api/health` → `git_sha`).

## 5. Bring up the stack (staged rollout)

**Stage A — api + auth first (UI testing).** Bring up Postgres, Redis, and the
backend only; hold the worker + sandbox until the LLM phase:

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d --scale worker=0 --scale pwa=0 \
  postgres redis backend
```

Confirm the migration ran and the health route responds:

```sh
# Entrypoint log should show "running database migrations" then "migrations complete".
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml logs backend | grep -i migrat

# Schema is at head:
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml exec postgres \
  psql -U bsvibe -d bsvibe -c "SELECT version_num FROM alembic_version;"

# Health route — the backend is published on host port 8700:
curl -s http://localhost:8700/api/health
# -> {"status":"ok","version":"0.1.0","git_sha":"<your sha>"}
```

The health route is `GET /api/health` (mounted in `backend/api/main.py` →
`backend/api/health.py`); it returns `{status, version, git_sha}`.

**Stage B — enable the worker + sandbox (LLM phase).** The work/verification
sandbox runs as a per-project container spawned **inside a Docker-in-Docker
sidecar** (`sandbox-dind`, behind the `sandbox` compose profile). The verifier's
declared `command` checks (pytest/ruff/uv) run inside that container — which
carries the toolchain the worker image deliberately lacks.

Set in `.env.prod` (already the default in `.env.prod.example`):

```sh
BSVIBE_SANDBOX_ENABLED=true
BSVIBE_DOCKER_HOST=tcp://sandbox-dind:2375   # the dind sidecar, internal network only
BSVIBE_SANDBOX_USER=0:0                       # match the worker's root-owned worktrees
```

Bring up the stack WITH the `sandbox` profile (starts the dind sidecar), then
build + **load the sandbox image into the dind daemon** — a plain host
`docker build` is invisible to the nested daemon, so it must be transferred:

```sh
# 1. Bring the stack up including the dind sidecar.
docker compose -p bsvibe-prod --profile sandbox \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d --scale worker=1

# 2. Build the toolchain image on the host AND load it into the dind daemon.
#    (build-sandbox-image.sh does `docker save | docker exec -i <dind> docker load`)
DIND_CONTAINER=bsvibe-sandbox-dind ./tools/build-sandbox-image.sh

# 3. Smoke-check the toolchain is reachable inside a spawned sandbox:
docker exec bsvibe-sandbox-dind \
  docker run --rm bsvibe-sandbox:latest python -m pytest --version
# -> pytest 8.x.x
```

Without the `--profile sandbox` flag the dind sidecar does **not** start and the
worker's `acquire()` fails fast (sandbox unavailable → run `system_error`),
never a silent host fallback.

The worker drains intake → agent → delivery → settle → relay. It connects to
the same DB; it does **not** run migrations (entrypoint overridden).

## 6. PWA (Vercel)

The PWA (`apps/pwa/`) is deployed **separately on Vercel** as a native Next.js
build — **not** `deploy/Dockerfile.pwa` (which exists only for an all-in-one
local/self-host option). Its only required env is:

- `NEXT_PUBLIC_BACKEND_URL` = the backend's **public** URL (e.g.
  `https://api.bsvibe.dev`).

Set it in the Vercel project settings and redeploy. The containerized `pwa`
service in compose is scaled to 0 by default for the Vercel path.

## 7. Operations

- `restart: unless-stopped` on every service — survives host reboot.
- Persistent volumes: `pgdata` (Postgres), `redisdata` (Redis), `appdata`
  (vault / skills / runs at `/app/var`).
- Postgres + Redis are **not** published to the host in prod — only the
  backend (8700) and, if used, the PWA (3700).
- Update: rebuild with a new `GIT_SHA`, `up -d` (the entrypoint re-runs
  `alembic upgrade head` idempotently on the new backend container).

## 8. Teardown

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml down
# Add -v to also drop the volumes (DESTROYS data):
# docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml down -v
```
