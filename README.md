# bsvibe-app

BSVibe AI agent OS — unified monorepo (PWA + FastAPI backend).

This is the Phase 0 skeleton. The full architecture context is in
`~/Docs/BSNexus/BSVibe_Workflow_Backend_2026-05-20.md` (§2.2 layout,
§12 Phase 0 spec).

## Prerequisites

- Docker (Desktop or compatible)
- [uv](https://docs.astral.sh/uv/) 0.5+
- Node 20+ with corepack
- pnpm 10 (via `corepack enable && corepack prepare pnpm@10.33.0 --activate`)

## Local stack

```sh
# 1. bring up Postgres + Redis + backend + PWA
docker compose -f deploy/compose.yaml up -d

# 2. probe /api/health (port 8700 — see ~/Works/_infra-phase0/port-map.md)
curl http://localhost:8700/api/health
# {"status":"ok","version":"0.1.0","git_sha":"dev"}

# 3. open the PWA placeholder
open http://localhost:3700/

# 4. shut down
docker compose -f deploy/compose.yaml down
```

## Running tests

```sh
# install deps once
uv sync --all-extras

# all tests (smoke skips if PG+Redis aren't reachable)
uv run pytest

# only the lifted shared/ unit tests (no infra needed)
uv run pytest tests/shared/

# coverage gate
uv run pytest --cov=backend --cov-fail-under=80

# lint + format + types
uv run ruff check backend/ tests/
uv run ruff format --check backend/ tests/
uv run mypy backend/

# PWA
cd apps/pwa
pnpm install
pnpm lint
pnpm typecheck
pnpm build
```

## Layout

```
backend/        FastAPI monolith + worker process types (single Python codebase)
  api/          HTTP handlers; /api/health is Phase 0's proof of life
  shared/       lifted bsvibe-core/authz/fastapi (Phase 0 first lift)
  {orchestrator,intake,delivery,execution,knowledge,plugins,
   skills,gateway,supervisor,workers,data}/   Phase 1+ stubs
apps/pwa/       Next.js 15 placeholder (Brief/Decisions/Inside come in Phase 2)
deploy/         Dockerfile.backend, Dockerfile.pwa, compose.yaml
.devcontainer/  fresh checkout -> uv sync && uv run pytest, zero manual steps
.github/        CI workflow
```

## Contributing

- All changes via PR against `main`.
- CI must pass (`lint-and-test` and `pwa` jobs) before merge.
- Branch protection on `main` is enforced — no direct pushes.
- Commit messages: `type(scope): subject` (e.g., `feat(api): add /api/health`).
  No `Co-Authored-By` lines.
- Use the worktree pattern: feature work lives under
  `~/Works/bsvibe-app/wt/<branch>/` (via
  `~/Works/_infra/scripts/create-worktree.sh bsvibe-app <branch>`),
  never directly in `~/Works/bsvibe-app/main/`.

## Phase 0 acceptance

The 8 criteria from §12.4 of the Workflow doc:

1. Repo + directory layout per §12.2.
2. CI green on `main`.
3. `docker compose up` + `curl /api/health` returns 200.
4. PG + Redis round-trip smoke test passes.
5. PWA placeholder login renders at `localhost:3700/`.
6. `backend/shared/` lift from `bsvibe-authz` + `bsvibe-fastapi` (+ transitive
   `bsvibe-core`) complete; ported tests pass.
7. Devcontainer: fresh checkout → `uv sync && uv run pytest` succeeds.
8. README documents the above.
