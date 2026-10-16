# A worker that cannot sign in to Claude alerts the founder

Outage 2026-10-15 14:26Z: the host worker's Claude OAuth refresh died (`invalid_grant`). Every
agent turn failed in ~1 s with no usage; the keepalive logged
`claude_auth_keepalive_definitive_failure` hourly for ~17 h and nobody was told. Found only
because a probe run (`096e64e2`) failed. Fixed by hand: `bsvibe-worker claude-login` + restart.

Now the keepalive's verdict rides every heartbeat as `X-BSVibe-Worker-Claude-Auth: ok|failed`
(a header — `HeartbeatBody` is `extra="forbid"`, a body field would 422 an older backend), and
the backend turns the transition into a `worker_auth_down` notification (default ON, like
`auth_down`), once per outage episode, plus an all-clear when it clears.

**Deploy order:** backend autodeploys (migration `worker_claude_auth_failed`, additive), THEN the
host worker: `git pull` in `~/Works/bsvibe-app/main` + `launchctl kickstart -k
gui/501/com.bsvibe.worker-mac-mini-e2e`. Either order is safe — an older backend ignores the
header, an older worker sends none.

## Unit / wire (local)

- [x] `failed` → one alert naming the worker and the fix (`claude-login`); a repeat → none
- [x] `ok` after `failed` → the all-clear (does not say "can't"); `ok` alone, no header → nothing
- [x] an unknown header value → ignored, heartbeat still 200
- [x] the real NotifyWorker delivers it to telegram — fresh workspace, AND a workspace whose
      prefs were saved before this event existed (key absent → default ON); an explicit off is respected
- [x] worker: keepalive records `failed` on a revoked grant (even when the CLI fallback lends a
      token, #970) and `ok` after a fresh sign-in; the heartbeat carries it; no verdict → no header
- [x] wire: the real `poll_and_execute` hands ONE state to the keepalive and to `run_once`
- [x] 9 wire cuts red (route call · down guard · up branch · default-on · keepalive failed/ok ·
      header · state to run_once · state to keepalive). `DEFAULT_MATRIX` entry is redundant with
      `DEFAULT_ON_EVENTS` (same as `auth_down`) and cutting it alone changes nothing

## Prod

- [ ] After deploy + worker restart: worker stays online (`bsvibe_workers_list` → `heartbeat_fresh`),
      backend logs no `worker_claude_auth_header_ignored`
- [ ] Next real refresh (~8 h after the 10-16 07:27Z sign-in): worker log `claude_oauth_refreshed`
- [ ] (When it next happens) a real `invalid_grant` → telegram "워커가 Claude 에 로그인하지 못해요",
      and after `claude-login` + restart the all-clear
