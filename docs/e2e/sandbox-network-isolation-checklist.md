# Sandbox network isolation — manual runbook (B0)

**The acute pre-launch risk is not internet exfil — it is internal SSRF + an
unauthenticated Docker-in-Docker escape.** A nested `bsvibe-sbx-*` container runs
UNTRUSTED code (the work agent's shell). By default it could reach the DinD
daemon's plaintext-unauthenticated `:2375` control socket on the bridge gateway
→ full Docker control → a privileged container mounting `appdata`
(`/app/var` = ALL tenants' data). No exfil needed. It could also reach
`postgres:5432` / `redis:6379` / `backend:8000` via NAT masquerade (SSRF).

This change closes that with **two layers**:

1. **In-daemon firewall** (`deploy/sandbox-dind-firewall.sh`, baked into
   `deploy/Dockerfile.sandbox-dind`): default-DROP for traffic originating on the
   nested bridge (`docker0`) to RFC1918 + link-local ranges + the daemon's
   `:2375`, while the **public internet stays open**. Every rule is scoped
   `-i docker0`, so the **backend→daemon management path (arriving on `eth0`) is
   untouched**.
2. **Network segmentation** (`deploy/compose.yaml`): `sandbox-dind` sits on a
   dedicated `sandboxnet` shared only with `backend` + `worker`, so the plaintext
   `:2375` socket is not on the same network as postgres/redis/pwa.

**CI cannot exercise nftables-in-DinD (no privileged Docker), so this runbook is
the enforcement proof.** Run it on the real Mac-Mini host after Stage B is up.

Static config is guarded in CI by
`tests/deploy/test_sandbox_network_isolation.py`; Redis auth by
`tests/shared/core/test_http.py::TestRedactUrlPassword`.

---

## Preconditions

Stage B is up with the `sandbox` profile and the hardened dind image built:

```sh
cd <repo root>
docker compose -p bsvibe-prod --profile sandbox \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --env-file deploy/.env.prod up -d --build --scale worker=1

# The firewall log line should appear in the dind sidecar:
docker logs bsvibe-sandbox-dind 2>&1 | grep sbx-fw
# EXPECT: [sbx-fw] nested-sandbox egress isolation applied (bridge=docker0, ...)
```

Confirm the rules are actually installed in the daemon's netns:

```sh
docker exec bsvibe-sandbox-dind iptables -w -S DOCKER-USER
# EXPECT: four "-A DOCKER-USER -d 10.0.0.0/8 -i docker0 -j DROP" style lines
#         (10/8, 172.16/12, 192.168/16, 169.254/16).
docker exec bsvibe-sandbox-dind iptables -w -S INPUT | grep 2375
# EXPECT: -A INPUT -i docker0 -p tcp -m tcp --dport 2375 -j DROP
```

Throwaway-container helper — a nested container on the **default** nested bridge,
exactly like a real sandbox (`docker run` with no `--network`):

```sh
# Discover the nested bridge gateway (the daemon's :2375 lives here):
GW=$(docker exec bsvibe-sandbox-dind docker network inspect bridge \
  -f '{{ (index .IPAM.Config 0).Gateway }}')
echo "nested gateway = $GW"
```

Each check below runs a short-lived `alpine` container **inside** the dind daemon
(`docker exec bsvibe-sandbox-dind docker run --rm ...`) — same network posture as
a `bsvibe-sbx-*` sandbox.

---

## (a) Nested container CANNOT reach internal/private destinations

Each command must **fail** (timeout / no route). `nc -w 3 -z` exits non-zero on
failure; we assert that.

- [ ] **Daemon control socket on the gateway (`:2375`) — the escape vector:**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 3 -z $GW 2375; echo exit=\$?"
  # EXPECT: exit=1  (blocked). A `exit=0` here is the container-escape hole — STOP.
  ```
  Stronger proof it cannot drive the daemon:
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "wget -T 3 -qO- http://$GW:2375/version; echo exit=\$?"
  # EXPECT: non-zero exit / timeout, NO JSON version payload.
  ```

- [ ] **Postgres (`postgres:5432`):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 3 -z postgres 5432 2>&1; echo exit=\$?"
  # EXPECT: exit=1 (name may also fail to resolve — either way, no connection).
  ```

- [ ] **Redis (`redis:6379`):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 3 -z redis 6379 2>&1; echo exit=\$?"
  # EXPECT: exit=1
  ```

- [ ] **Backend (`backend:8000`):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 3 -z backend 8000 2>&1; echo exit=\$?"
  # EXPECT: exit=1
  ```

- [ ] **A raw RFC1918 IP (belt-and-suspenders, in case DNS is bypassed):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 3 -z 10.0.0.1 5432; echo exit=\$?; nc -w 3 -z 192.168.0.1 80; echo exit=\$?"
  # EXPECT: exit=1 for both.
  ```

## (b) Nested container CAN still reach the public internet

Must **succeed** — `uv sync`/PyPI, npm, and founder external-API tasks depend on
it.

- [ ] **PyPI (the real `uv sync` dependency surface):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "wget -T 10 -qO- https://pypi.org/simple/pip/ | head -c 100; echo; echo exit=\$?"
  # EXPECT: HTML output + exit=0.
  ```

- [ ] **Public DNS + TCP 443 to a CDN:**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm alpine \
    sh -c "nc -w 5 -z files.pythonhosted.org 443; echo exit=\$?"
  # EXPECT: exit=0
  ```

## (c) Backend can STILL create + manage sandboxes, and a real run verifies

The firewall rules are scoped to the nested bridge, so the backend→`:2375` path
(arriving on `eth0`) is untouched. Prove it end-to-end.

- [ ] **Backend→daemon path works (the load-bearing separation):**
  ```sh
  # From the backend container — NOT nested — talk to the daemon over :2375.
  docker compose -p bsvibe-prod -f deploy/compose.yaml -f deploy/compose.prod.yaml \
    exec backend sh -c "wget -T 5 -qO- http://sandbox-dind:2375/version | head -c 200; echo"
  # EXPECT: JSON with a Docker server "Version" — the daemon answers the backend.
  ```

- [ ] **The toolchain smoke still passes (README §5 Stage B step 3):**
  ```sh
  docker exec bsvibe-sandbox-dind docker run --rm bsvibe-sandbox:latest \
    python -m pytest --version
  # EXPECT: pytest 8.x.x
  ```

- [ ] **A real run's verify works.** Trigger one work/verify run (PWA or MCP) on a
  product whose checks run in the sandbox (e.g. a pytest/ruff project). Watch:
  ```sh
  docker compose -p bsvibe-prod -f deploy/compose.yaml -f deploy/compose.prod.yaml \
    logs -f worker | grep -E "sandbox_created|verification|declared"
  # EXPECT: sandbox_created for the project, declared checks run INSIDE the
  #         sandbox, and the run reaches a verified/failed verdict (NOT
  #         system_error / SandboxUnavailable). A `uv sync` step inside the run
  #         must still succeed — proving public egress survived.
  ```

## (d) Rollback (reversible)

The change is contained to the deploy artifacts. To revert to the previous
(unhardened) behavior without a full redeploy:

- [ ] **Fast, in-place — drop the firewall rules on the running daemon:**
  ```sh
  # Remove the INPUT :2375 block and the four DOCKER-USER private-range drops.
  docker exec bsvibe-sandbox-dind sh -c '
    iptables -w -D INPUT -i docker0 -p tcp --dport 2375 -j DROP 2>/dev/null;
    for c in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
      iptables -w -D DOCKER-USER -i docker0 -d $c -j DROP 2>/dev/null;
    done; echo reverted'
  # NOTE: rules reapply on the next dind restart (the wrapper runs at startup).
  ```

- [ ] **Full rollback — redeploy the previous image tag / revert the PR:** the
  only code touched is `deploy/` + a logging redaction helper. Revert the commit,
  rebuild, `up -d`. Network segmentation and Redis auth revert with it. (If you
  keep Redis auth but drop the URL default, ensure `BSVIBE_REDIS_URL` still
  carries the password, else the app 500s with NOAUTH.)

---

## What this does NOT cover (residual, accepted)

- **The `:2375` socket is still plaintext + unauthenticated on `sandboxnet`.**
  Part 1 (the firewall) is the primary control against the *nested* escape;
  network segmentation reduces the socket's reachability to backend+worker only.
  Full mutual-TLS + client-cert on the daemon is a larger lift deferred
  post-launch. Residual: a compromise of the `backend` or `worker` container
  itself still reaches `:2375`. That is a strictly smaller surface than a
  nested-sandbox escape (untrusted code runs in the nested sandbox by design; it
  does not run in backend/worker).
- **Firewall enforcement is runbook-verified, not CI-verified** — CI has no
  privileged Docker. CI covers only the static rule text + Redis auth wiring.
