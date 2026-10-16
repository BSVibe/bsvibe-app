# Billing — capacity subscription (#928, gate 4)

## Decisions (형님, 2026-10-16)

| | |
|---|---|
| **Model** | Capacity subscription. Model tokens stay BYO (the workspace's own provider key, or its own worker on its own subscription). BSVibe sells platform capacity, not tokens |
| **Payment** | Merchant of Record — **Paddle** recommended (handles global VAT/invoicing for a Korean seller; Lemon Squeezy is Stripe-owned since 2024 and its direction is unclear) |
| **Free** | 3 concurrent runs · 1 verification stack · 50M tokens/month |
| **Pro** | 10 concurrent runs · 2 verification stacks · 50M tokens/month |

The monthly token budget is a **safety quota**, not a price lever (`workspaces_db.py`) — the
paid plan does not raise it. Price is set in Paddle, not in code.

## What the platform actually pays for

Measured 2026-10-15 (code survey): every chat/agent completion resolves to the workspace's
own model account (`dispatch/resolver.py` never falls back to a platform key). The platform
pays for: server compute (sandbox + verification containers), the deployment-wide embedding
model (host Ollama), storage. Those are what the plan limits bound.

## Steps

1. **Plan entitlements** ✅ (#1164) — `workspaces.plan` + `backend.identity.plans.apply_plan`,
   the ONE writer of the plan and the limits it sets. Existing uncapped workspace → `operator`.
2. **Paddle webhook** ✅ — `POST /api/webhooks/paddle` (setup: `docs/e2e/paddle-subscription-webhook-checklist.md`):
   - verify `Paddle-Signature` (HMAC, secret via pydantic-settings — never logged)
   - idempotent on the event id (store processed ids, like `delivery_events`)
   - status `active`/`trialing` on a Pro price → `apply_plan(PRO)`; `canceled`/`paused` →
     `apply_plan(FREE)`; `past_due` → plan kept (Paddle is still collecting and sends
     `canceled` if it gives up)
   - the FIRST event names the workspace via `custom_data.workspace_id` and binds the
     subscription id; later events resolve by that id — client-set `custom_data` cannot
     re-point a paid subscription
   - out-of-order deliveries: an event older than the last applied (`billing_event_at`) is
     ignored; the operator workspace is never moved by a payment
3. **Checkout + portal** — PWA "Upgrade" opens Paddle.js overlay with
   `customData: {workspace_id}`; "Manage billing" opens the customer portal. Replaces the
   `bsvibe.dev/{locale}/pricing` link the run-cap refusal shows today
   (`apps/pwa/components/shell/DirectAction.tsx`)
4. **Surface the plan** — `bsvibe_workspace_get` / Settings show the plan and its limits

## Rules that hold across steps

- A downgrade never kills held work: caps refuse the NEXT submission only (`run_caps` counts
  at admission).
- Only `apply_plan` writes plan + limits — an operator moves a workspace through it too.
- Founder-only (owner role) for checkout/portal.
- The repo is public: no Paddle secrets, price ids are config.
