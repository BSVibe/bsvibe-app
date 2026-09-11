#!/usr/bin/env bash
# Off-box liveness probe — 게이트 3. Names WHICH layer is broken.
#
# Two readings, because one was not enough:
#
#   shallow  GET  /api/health        edge + tunnel + app process
#   deep     POST /api/auth/login    the same PLUS the auth dependency
#
# ``/api/health`` is ``return HealthResponse(status="ok", version=…, git_sha=…)``
# — settings in, settings out. On 2026-09-11 Supabase was paused and login was
# broken while that route answered 200 through the whole outage. A watch that
# green-lights an app whose auth is dead converts an outage into confidence.
#
# On the deep probe a 4xx is HEALTHY: it means the app processed the request and
# its dependency answered (invalid credentials is the expected verdict for the
# throwaway account below). Only 5xx / no-connection mean the dependency is gone.
# The credentials are deliberately bogus and the call is read-only — nothing is
# created, and no real account can be locked out by it.
#
# Lives here as a script rather than inline in the workflow so the verdict logic
# is reachable from a test (tests/infra/test_offbox_probe.py). A verdict nobody
# can reach from a test is a verdict that is never checked.
set -uo pipefail

BASE_URL="${BASE_URL:-https://api.bsvibe.dev}"
RETRY_DELAY="${RETRY_DELAY:-30}"
HEALTH_PATH=/api/health
LOGIN_PATH=/api/auth/login

# NOTE: no ``|| echo 000`` after curl -w. curl already prints the code, so the
# fallback CONCATENATES into "000000" — a value that matches no branch below and
# sails through. That bug has appeared three times in this repo's probes;
# ``${x:-000}`` is the correct empty-guard.
probe_health() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE_URL$HEALTH_PATH" 2>/dev/null)"
  printf '%s' "${code:-000}"
}

probe_deep() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    -X POST -H 'Content-Type: application/json' \
    --data '{"email":"offbox-probe@invalid.example","password":"not-a-real-password"}' \
    "$BASE_URL$LOGIN_PATH" 2>/dev/null)"
  printf '%s' "${code:-000}"
}

# Is the edge (Cloudflare) refusing us rather than the origin failing? A 403 that
# carries ``cf-mitigated`` is a WAF verdict on the CLIENT, not an outage — saying
# "prod is down" would send someone to restart a healthy backend.
edge_challenged() {
  curl -s -D - -o /dev/null --max-time 20 "$BASE_URL$HEALTH_PATH" 2>/dev/null \
    | grep -qi '^cf-mitigated:'
}

verdict() { # health_code deep_code -> prints verdict, returns exit status
  local h="$1" d="$2"
  if [ "$h" = "000" ] && [ "$d" = "000" ]; then
    echo "UNREACHABLE — nothing answered at $BASE_URL (box, tunnel or DNS). health=$h login=$d"
    return 1
  fi
  if [ "$h" = "403" ] && edge_challenged; then
    echo "EDGE — the edge refused this client (cf-mitigated). The origin was never asked. health=$h"
    return 1
  fi
  if [ "$h" != "200" ]; then
    echo "APP — $HEALTH_PATH did not answer 200 (edge/tunnel/app process). health=$h login=$d"
    return 1
  fi
  case "$d" in
    2??|4??)
      echo "OK — $HEALTH_PATH 200 and $LOGIN_PATH $d (the auth dependency answered)"
      return 0
      ;;
  esac
  echo "DEPENDENCY — the app is fine ($HEALTH_PATH 200) but $LOGIN_PATH returned $d: its auth dependency (Supabase) is down. Do NOT restart the backend."
  return 1
}

h="$(probe_health)"; d="$(probe_deep)"
echo "probe 1: health=$h login=$d"
out="$(verdict "$h" "$d")"; rc=$?
if [ "$rc" = "0" ]; then echo "$out"; exit 0; fi

# One bad reading is not an outage: runners blip, and a deploy restarts the
# backend for a few seconds. Confirm before alerting so this stays believable.
echo "first reading unhealthy — re-checking in ${RETRY_DELAY}s before alerting"
sleep "$RETRY_DELAY"
h="$(probe_health)"; d="$(probe_deep)"
echo "probe 2: health=$h login=$d"
out="$(verdict "$h" "$d")"; rc=$?
if [ "$rc" = "0" ]; then echo "recovered on the second reading — $out"; exit 0; fi
echo "::error::$out"
exit 1
