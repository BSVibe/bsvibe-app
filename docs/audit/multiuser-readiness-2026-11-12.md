# BSVibe multi-user readiness re-audit — 2026-11-12

Re-measures [`multiuser-readiness-2026-09-10.md`](./multiuser-readiness-2026-09-10.md) against `main` at `44b7813b`.
Five dimensions, one auditor and one adversarial verifier each (105 claims checked, 7 corrected). Read-only: code,
infra scripts, `docker ps` / `docker logs`; no production database, no `docker exec`, no tests against a live DB.

## Verdict

**Still no — do not open sign-up to strangers yet.** The reason has moved. The 09-10 security High set is closed or
reduced (H1, H2, H4 resolved; H3 partly), RLS now fails closed with a per-transaction GUC under a non-BYPASSRLS role,
background workers run tenant-scoped, run-scoped MCP tokens are bound to the run's claim, and the run cap and token
budget meet webhook intake. What blocks now is mostly surface that did not exist on 09-10, plus limits that are per
workspace while the scarce resources are global.

**Security findings are tracked outside this public document until they are fixed and deployed** (this repository is
public). The count: three blocker and two high, all cross-tenant; issues are filed with neutral titles.

## Ⅰ. Security and tenant isolation

| 09-10 item | Now |
|---|---|
| H1 worker result closes any task | resolved — owner and state checked |
| H2 global unclaimed install pool | resolved — claimed by installation ref |
| H3 global OAuth app credentials writable | partly — see tracked findings |
| H4 Telegram token in exception text | resolved (a transport-error residue remains, low) |
| Viewer can change Safe Mode / delete workspace | resolved on REST; the MCP twin has no role check (latent: no invite flow yet, every member is an owner) |
| RLS on 6 tables, fail-open without GUC | partly — fail-closed now; still 6 of 47 tables |
| GUC survives on pooled connections | resolved |
| Public auth surface (introspect, device flow, SSE token in query, no login/reset limit) | partly — per-IP limits on OAuth endpoints; sign-up / login / reset have none |
| Credential storage | partly — key rotation resolved; a plaintext secret in `delivery_config`, no reaper for pending/unclaimed OAuth rows |
| Flat `var/` without a workspace level | still open (matters when the sandbox is off) |
| ORM auto-filter SELECT-only | still open; also a no-op when no workspace is bound |
| Background workers without tenant scope | resolved |

New (non-disclosing): product-declared secrets are injected into the sandbox but not redacted from shell output
(medium); one privileged DinD is shared by every tenant with a memory limit only (medium).

## Ⅱ. Scale and concurrency

- **2 global drive slots and 1 machine-wide verify slot**, claimed FIFO by `created_at` with no per-workspace fairness,
  while plans state 3 / 10 / unlimited concurrent runs and up to 2 verify stacks. The knobs are not passed through prod
  compose. (high)
- Capacity yield-back returns a run to the head of the global queue; held webhook triggers are released oldest-first
  under a global limit applied before filtering — one tenant's backlog can stall the others. (high)
- An in-place run that loses the verify-slot race records a FAILED verification instead of waiting. (high)
- Saturation is taken from the worker's own heartbeat value. DB pools are SQLAlchemy defaults. DeliveryWorker's
  poison-pill counter is in memory. Connector import runs inside the request. No rate-limit / body-size middleware.

## Ⅲ. Onboarding

Improved: email sign-up, the curl worker installer, first API-key account becomes the default, monthly budget
unlimited and self-served, needs_you notifications with inline answers. Still blocking:

- The GitHub App is created **private**, so a stranger cannot install it on their own repositories. (high)
- Sign-up confirmation mail runs on Supabase's built-in SMTP rate limit; sign-up has no abuse control. (high)
- Product-before-model-account order leaves the bootstrap without a PWA retry (a REST retry exists). (medium)
- The free plan's settings tab still links to paid plans — conflicts with the 2026-10-16 "no upgrade copy" decision.
- Onboarding copy tells API-key users to install a worker they do not need; no terms/privacy acceptance at sign-up.

## Ⅳ. Operations and availability

- **2026-11-05 13:40 → 11-11 02:24 UTC: PWA sign-in and sessions were down for 5.5 days** (Supabase JWKS host did not
  resolve). The heartbeat, the off-box GitHub monitor and the in-app `auth_down` notice were all red; nobody responded.
  The alert path is one person. (blocker for opening)
- The key that decrypts every workspace's credentials (`BSVIBE_GATEWAY_KMS_KEY_B64`) and the MCP OAuth signing key have
  no documented off-box copy. (high)
- Restore runbook never rehearsed and stale; recovery point is 24 h (nightly dump, no WAL archiving). No post-deploy
  health gate or rollback; logs kept about 6 days; no error tracking; no status notice for non-founder users.
- No resource limits on any prod container; prod shares the box with dev and other projects.
- Deploys defer on any tenant's live run, up to the deferral cap, then kill in-flight drives.

## Ⅴ. Platform resources (tokens are BYO; this is the box)

- **Schedules**: no count, interval floor or overlap guard, outside the run cap, and they keep firing after the
  workspace is soft-deleted. (blocker)
- **Unlimited workspaces per user** multiply every per-workspace limit. (blocker)
- Sandbox caps memory only (no CPU / PID / disk); the per-process semaphore lets API and worker each start boxes.
- `intents_create` embeds an unbounded example list inside the request on the platform's embedding model.
- No per-workspace quota on knowledge volume / embeddings; no retention for per-run tables; deleted workspaces never
  purged; product clones unbounded; no Docker log rotation.
- MCP `bsvibe_connectors_import_now` fails in prod (missing `session` argument).

## Ⅵ. Order of repair

1. **Founder, now**: rotate the R2 product-bundle key (printed into a local session transcript during this audit —
   nothing left the machine); confirm off-box copies of the KMS key and the OAuth signing key; decide where an auth
   outage alert must reach.
2. **Security (tracked)** — the five cross-tenant findings.
3. **Resources** — schedule limits and lifecycle; a per-user workspace limit.
4. **Scale** — make drive/verify capacity configurable in prod and fair across workspaces; plans that match it; the
   verify-slot race.
5. **Onboarding** — a public GitHub App, SMTP, sign-up abuse control, copy.
6. **Operations** — a restore rehearsal, a deploy health gate, log retention, a second alert path.

The full per-claim evidence (file:line and commands) is kept off GitHub with the founder.
