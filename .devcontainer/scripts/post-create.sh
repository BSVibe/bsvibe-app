#!/usr/bin/env bash
set -euo pipefail

cd /workspace
uv sync --all-extras

if [ -d apps/pwa ]; then
  cd apps/pwa
  pnpm install --frozen-lockfile || pnpm install
fi
