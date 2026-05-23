#!/bin/sh
# BSVibe backend container entrypoint.
#
# Runs database migrations ONCE, then execs the API server. Only the API
# (backend) service uses this entrypoint — the worker service starts directly
# (`python -m backend.workers`) so the two never race on `alembic upgrade head`.
# Alembic is idempotent (it short-circuits when alembic_version is already at
# head), so a restart of the API container is safe.
set -eu

echo "[entrypoint] running database migrations (alembic upgrade head)..."
uv run alembic upgrade head
echo "[entrypoint] migrations complete; starting server."

exec "$@"
