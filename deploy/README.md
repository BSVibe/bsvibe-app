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
32-byte key in **URL-safe** base64. `backend/accounts/crypto.py` does
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

- `BSVIBE_DB_PASSWORD` — must match the password embedded in `BSVIBE_DATABASE_URL`.
- `BSVIBE_DATABASE_URL` — `postgresql+asyncpg://bsvibe:<password>@postgres:5432/bsvibe`
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

**Stage B — enable the worker + sandbox (LLM phase).** Once a `ModelAccount`
is configured and you've built the sandbox image, scale the worker up:

```sh
# Build the sandbox image the worker spawns for LLM execution:
docker build -f deploy/Dockerfile.sandbox -t bsvibe-sandbox:latest .

# In .env.prod: BSVIBE_SANDBOX_ENABLED=true (+ BSVIBE_DOCKER_HOST if remote).
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d --scale worker=1
```

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
