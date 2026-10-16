# Paddle subscription webhook → workspace plan (#928 step 2)

`POST /api/webhooks/paddle` verifies `Paddle-Signature`, then moves the workspace through
`apply_plan`: active/trialing on a Pro price → Pro, canceled/paused → Free, past_due → no change.
Backend only — autodeployed; migration `workspace_billing` (additive).

## Unit / wire (local)

- [x] signed activation → Pro (10 runs · 2 slots), subscription + customer ids stored
- [x] forged / stale / tampered → 401, nothing moves; no secret → 404
- [x] cancel → Free; past_due → plan kept, status recorded; unknown price → nothing
- [x] operator workspace never moved; a later event cannot re-point a bound subscription
- [x] unknown workspace / no workspace → 200, nothing (a retry cannot fix it)
- [x] replayed event id applies once; an older event arriving late is ignored
- [x] 10 wire cuts red on a throwaway PG with fail-closed RLS (`bsvibe_app` role) — incl. the cross-tenant read and the scoped write

## 형님 — Paddle setup (sandbox first)

1. Paddle sandbox account → **Catalog**: product "BSVibe Pro", prices monthly (+ yearly)
2. **Developer tools → Notifications → New destination**
   - URL: `https://api.bsvibe.dev/api/webhooks/paddle`
   - Events: `subscription.created`, `subscription.activated`, `subscription.updated`,
     `subscription.canceled`, `subscription.paused`, `subscription.resumed`, `subscription.past_due`
3. `.env.prod` (never committed):
   - `BSVIBE_PADDLE_WEBHOOK_SECRET=<destination secret key>`
   - `BSVIBE_PADDLE_PRO_PRICE_IDS=pri_…,pri_…`
   - recreate backend (autodeploy does on next merge, or `docker compose up -d --force-recreate backend`)

## Prod (sandbox) walk

- [ ] Paddle "Send test event" with a bad secret configured → backend log `paddle_webhook_signature_rejected`
- [ ] Sandbox checkout with `customData: {workspace_id: <a test workspace>}` → log
      `paddle_webhook_applied plan=pro`; that workspace now accepts a 4th concurrent run
- [ ] Cancel the sandbox subscription → `plan=free`
- [ ] Before step 3 (PWA checkout) ships, checkout needs `customData.workspace_id` set by hand
