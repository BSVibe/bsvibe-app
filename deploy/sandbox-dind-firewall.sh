#!/bin/sh
# BSVibe sandbox-dind firewall — nested-sandbox egress isolation.
#
# WHY THIS EXISTS
# ---------------
# `sandbox-dind` is a privileged Docker-in-Docker daemon. The worker spawns a
# per-project `bsvibe-sbx-*` container INSIDE it (over tcp://sandbox-dind:2375)
# to run the LLM's shell_exec + the verifier's declared checks. Those nested
# containers run UNTRUSTED code (the work agent's shell commands).
#
# By default a nested container can:
#   * reach the daemon's own control socket on the nested bridge gateway
#     (<gateway>:2375) — PLAINTEXT + UNAUTHENTICATED → full Docker control →
#     run a privileged container mounting `appdata` (/app/var = ALL tenants'
#     data). This is a container escape / total compromise, no exfil needed.
#   * reach internal services reachable from this daemon's own netns
#     (postgres:5432, redis:6379, backend:8000) via NAT masquerade — internal
#     SSRF.
#
# WHAT THIS BLOCKS
# ----------------
# Default-DROP for traffic ORIGINATING FROM the nested bridge (`docker0` inside
# this DinD) to private/internal destinations, while leaving the PUBLIC
# internet OPEN (so `uv sync`/PyPI, npm, and founder external-API tasks keep
# working). Private ranges are STATIC (RFC1918 + link-local + the DinD gateway).
#
# WHAT THIS DOES NOT TOUCH
# ------------------------
# The backend->daemon management path (backend/worker container ->
# sandbox-dind:2375) arrives on the DinD's EXTERNAL interface (eth0, the compose
# network), NOT on the nested bridge `docker0`. Every rule below matches
# `-i docker0` (in-interface = nested bridge), so the backend's control path is
# untouched. This is the load-bearing separation.
#
# HOW IT IS APPLIED
# -----------------
# compose cannot model iptables. dockerd creates `docker0` + the `DOCKER-USER`
# chain only AFTER it starts, so we launch a background waiter that blocks until
# they exist, applies the rules, then leaves. Meanwhile we exec the stock
# `dockerd-entrypoint.sh` (the docker:dind image's real entrypoint — it does TLS
# scaffolding, iptables enablement, storage setup) as PID 1 with the original
# args. NEVER replace that entrypoint; wrap it.
#
# WHY IT FAILS CLOSED (2026-08-30)
# --------------------------------
# The waiter runs in the BACKGROUND, so its exit status is structurally
# unreadable — nothing waits on it. Before this change a failure to apply (chain
# never appeared, iptables unavailable, rules silently dropped by another layer)
# left the daemon serving nested containers with NO isolation, and the only
# trace was one stderr line. A nested container in that state reaches
# postgres/redis/backend and the :2375 control socket — every tenant's data plus
# container escape.
#
# So two things changed:
#   1. VERIFY after applying. `-I` returning 0 means "the command was accepted",
#      NOT "the rule is in the table now". Each rule is re-checked with `-C`.
#   2. On any failure, KILL PID 1. A daemon that cannot prove its isolation must
#      not accept work — dying loudly beats serving silently unprotected.
#
# `SBX_FW_SELFTEST=1` runs apply+verify and exits with that status instead of
# exec'ing dockerd, so CI can prove this behaviour against a stubbed `iptables`
# on PATH (there is no privileged Docker in CI).
set -eu

NESTED_BRIDGE="docker0"
# The RFC1918 + link-local ranges. STATIC. A nested container must not reach any
# of these: the compose subnet (postgres/redis/backend + the DinD's own eth0)
# and the nested bridge gateway all live inside them.
PRIVATE_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16"
DAEMON_PORT="2375"
# Wait budget (tries ≈ seconds). Overridable so CI can run the selftest fast.
WAIT_TRIES="${SBX_FW_WAIT_TRIES:-120}"

apply_firewall() {
    # Wait for the nested bridge to appear (dockerd creates it on start).
    i=0
    while ! ip link show "$NESTED_BRIDGE" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge "$WAIT_TRIES" ]; then
            echo "[sbx-fw] FATAL: $NESTED_BRIDGE never appeared" >&2
            return 1
        fi
        sleep 1
    done

    # Wait for Docker's DOCKER-USER chain (Docker guarantees it is evaluated in
    # FORWARD before Docker's own rules and is NEVER flushed by Docker — the
    # canonical place for operator FORWARD rules).
    i=0
    while ! iptables -w -n -L DOCKER-USER >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge "$WAIT_TRIES" ]; then
            echo "[sbx-fw] FATAL: DOCKER-USER chain never appeared" >&2
            return 1
        fi
        sleep 1
    done

    # (a) FORWARD path: nested container -> internal/private destinations.
    #     Routed traffic from `docker0` out to any private range is DROPPED.
    #     Public destinations (PyPI/npm CDNs, founder APIs) are NOT in these
    #     ranges, so they fall through and are masqueraded out to the internet.
    for cidr in $PRIVATE_RANGES; do
        if ! iptables -w -C DOCKER-USER -i "$NESTED_BRIDGE" -d "$cidr" -j DROP 2>/dev/null; then
            iptables -w -I DOCKER-USER -i "$NESTED_BRIDGE" -d "$cidr" -j DROP
        fi
    done

    # (b) INPUT path: nested container -> the daemon's own control socket on the
    #     bridge gateway (<gateway>:2375). This is destined to the DinD itself
    #     (INPUT, not FORWARD), so DOCKER-USER does not cover it. Drop 2375
    #     arriving on the nested bridge. The backend's path (arriving on eth0) is
    #     untouched.
    if ! iptables -w -C INPUT -i "$NESTED_BRIDGE" -p tcp --dport "$DAEMON_PORT" -j DROP 2>/dev/null; then
        iptables -w -I INPUT -i "$NESTED_BRIDGE" -p tcp --dport "$DAEMON_PORT" -j DROP
    fi

    echo "[sbx-fw] nested-sandbox egress isolation applied (bridge=$NESTED_BRIDGE," \
        "private ranges dropped, :$DAEMON_PORT blocked from nested)" >&2
}

# Applying is not the same proposition as being applied. `-I` returns 0 when the
# command was accepted; it says nothing about the table's state afterwards.
verify_firewall() {
    missing=""
    for cidr in $PRIVATE_RANGES; do
        iptables -w -C DOCKER-USER -i "$NESTED_BRIDGE" -d "$cidr" -j DROP 2>/dev/null \
            || missing="$missing FORWARD:$cidr"
    done
    iptables -w -C INPUT -i "$NESTED_BRIDGE" -p tcp --dport "$DAEMON_PORT" -j DROP 2>/dev/null \
        || missing="$missing INPUT:$DAEMON_PORT"

    if [ -n "$missing" ]; then
        echo "[sbx-fw] FATAL: rules absent after apply —$missing" >&2
        return 1
    fi
    echo "[sbx-fw] verified: every isolation rule is present" >&2
}

guard() {
    apply_firewall && verify_firewall
}

if [ "${SBX_FW_SELFTEST:-}" = "1" ]; then
    # CI path: prove the guard's behaviour against a stubbed iptables, then stop.
    # Never exec dockerd here — there is no daemon to run in a test.
    guard
    exit $?
fi

# Run the setup in the background so it can wait for dockerd without blocking
# dockerd itself. Its exit status is unreadable from here, so on failure it
# terminates PID 1 directly: a daemon that cannot prove its isolation must not
# accept nested containers.
{
    if ! guard; then
        echo "[sbx-fw] FATAL: isolation could not be established — killing the daemon" >&2
        kill 1
    fi
} &

exec dockerd-entrypoint.sh "$@"
