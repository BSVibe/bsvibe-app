#!/usr/bin/env bash
# Build the bsvibe-sandbox toolchain image and load it INTO the DinD
# sidecar. A plain `docker build` produces the image on the *host*
# daemon; the per-project sandboxes run inside the DinD, whose daemon
# has its own image store and cannot see host images — so the image
# must be transferred across.
#
# The transfer uses `docker save | docker exec -i <dind> docker load`,
# NOT `DOCKER_HOST=tcp://... docker load`: the prod DinD sidecar does
# not publish its 2375 port, so it is unreachable by URL from the host
# — but it IS reachable by container name via `docker exec`.
#
# Idempotent: safe to run on every deploy / image refresh (cheap when
# layers are cached).
#
# Env:
#   DIND_CONTAINER  name of the DinD sidecar container
#                   (default: bsvibe-sandbox-dind)
#   SANDBOX_IMAGE   image tag (default: bsvibe-sandbox:latest)
set -euo pipefail

IMAGE="${SANDBOX_IMAGE:-bsvibe-sandbox:latest}"
DIND="${DIND_CONTAINER:-bsvibe-sandbox-dind}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="${SCRIPT_DIR}/../deploy/Dockerfile.sandbox"

if ! docker inspect "${DIND}" >/dev/null 2>&1; then
  echo "ERROR: DinD sidecar container '${DIND}' not found." >&2
  echo "       Bring the stack up first, or set DIND_CONTAINER." >&2
  exit 1
fi

echo "==> Building ${IMAGE} on the host daemon"
docker build -f "${DOCKERFILE}" -t "${IMAGE}" "${SCRIPT_DIR}/.."

echo "==> Loading ${IMAGE} into the DinD daemon (container ${DIND})"
docker save "${IMAGE}" | docker exec -i "${DIND}" docker load

echo "==> Done. ${IMAGE} is available inside the DinD."
