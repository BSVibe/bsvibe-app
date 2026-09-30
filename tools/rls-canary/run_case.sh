#!/bin/bash
# Differential RLS canary — one case: restore a prod dump into a throwaway PG,
# optionally close the policy, run canary.py from <checkout>, print row deltas.
#
#   run_case.sh <label> <prod.dump> <checkout> <open|closed>
#
# Compare an `open` and a `closed` case on the SAME dump and code: the row
# deltas must be identical. Delete the dump afterwards — it is prod data.
set -euo pipefail
label=$1; dump=$2; checkout=$3; mode=$4
here=$(cd "$(dirname "$0")" && pwd)
name=pg-rls-canary; port=${CANARY_PORT:-5469}
docker rm -f $name >/dev/null 2>&1 || true
docker run -d --name $name -e POSTGRES_PASSWORD=bsvibe -e POSTGRES_USER=bsvibe \
  -e POSTGRES_DB=bsvibe -p $port:5432 pgvector/pgvector:pg16 >/dev/null
until docker exec $name pg_isready -U bsvibe >/dev/null 2>&1; do sleep 2; done; sleep 2
docker exec $name psql -U bsvibe -d bsvibe -qc \
  "create role bsvibe_app login password 'bsvibe_app_ci' nosuperuser nobypassrls"
docker exec -i $name pg_restore -U bsvibe -d bsvibe --no-owner --role=bsvibe < "$dump" >/dev/null 2>&1 || true
if [ "$mode" = closed ]; then
  docker exec -i $name psql -U bsvibe -d bsvibe -v ON_ERROR_STOP=1 -q < "$here/failclosed.sql"
fi
escape=$(docker exec $name psql -U bsvibe -d bsvibe -Atc \
  "select count(*) from pg_policies where coalesce(qual,'')||coalesce(with_check,'') like '%IS NULL%'")
out=$(cd "$checkout" && CANARY_APP_URL="postgresql+asyncpg://bsvibe_app:bsvibe_app_ci@localhost:$port/bsvibe" \
  CANARY_OWNER_URL="postgresql+asyncpg://bsvibe:bsvibe@localhost:$port/bsvibe" \
  uv run python "$here/canary.py" 2>&1 | tail -1)
echo "$label|$(git -C "$checkout" log --oneline -1 | cut -c1-7)|$mode|escape=$escape|$out"
docker rm -f $name >/dev/null
