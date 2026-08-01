# E2E — `bsvibe-worker service install|uninstall|status`

GitHub-Actions-runner-style durability: the self-hosted executor worker survives
crashes + reboots via the OS service manager, installed with one command. BSVibe
provides the tooling; the user keeps their worker alive (not a managed worker).

## Behavior (unit-verified)
- [x] `service install` requires a registered worker (`~/.bsvibe/config.json`) — else exits 1 with a hint.
- [x] Renders launchd plist (macOS, `KeepAlive`+`RunAtLoad`) / systemd unit (Linux, `Restart=always`) from the saved config.
- [x] Unit file written mode `0600`; **no `BSVIBE_WORKER_TOKEN` in the unit** (worker reads `~/.bsvibe/worker.token`).
- [x] `install` runs `launchctl bootstrap`+`kickstart` / `systemctl --user daemon-reload`+`enable --now`; `uninstall` runs `bootout` / `disable --now` + removes the unit.

## Live E2E (macOS Mac Mini — the founder's dogfood host)
> This swaps the current hand-started executor for a launchd-managed one. Do it in a quiet window (no in-flight runs) — a brief gap is covered by the stale-claim reaper.
- [ ] `cd ~/Works/bsvibe-app/main && ./.venv/bin/bsvibe-worker service install`
- [ ] `launchctl print gui/$(id -u)/com.bsvibe.worker` shows the job loaded, `state = running`.
- [ ] Confirm the plist is mode `0600` and contains NO token: `stat -f '%Lp' ~/Library/LaunchAgents/com.bsvibe.worker.plist` = 600; `grep -c TOKEN ~/Library/LaunchAgents/com.bsvibe.worker.plist` = 0.
- [ ] `executor_workers` shows the managed worker heartbeating (fresh `last_heartbeat`); a dispatched run completes end-to-end.
- [ ] **Auto-restart proof**: `kill` the worker process → launchd restarts it within `ThrottleInterval` (10s); heartbeat resumes with no manual action. (The watchdog would also alert if it stayed down.)
- [ ] Stop the OLD hand-started process (pid from `pgrep -f 'bsvibe-worker run'` that is NOT the launchd child) so only the managed one remains; deregister stale test workers.
- [ ] Reboot test (optional): after a reboot/login the worker auto-starts.

## Notes
- Closes the audit findings: unsupervised executor SPOF (now auto-restarts) + plaintext worker token in a world-readable plist (now token-free, mode 0600).
- Replaces the manual `{PLACEHOLDER}` example templates (kept as a manual-reference fallback).
