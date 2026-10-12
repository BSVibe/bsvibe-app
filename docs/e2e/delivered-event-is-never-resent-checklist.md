# A delivered event is never resent — E2E checklist

Incident 2026-10-12: the weekly BStockReport schedule run `e9a2ef9a` emitted a
partial deliverable with `artifact_type="report"`. The delivery worker sent it to
Telegram, then `DeliveryResult(artifact_type="report")` raised. The
`delivery_events` row stayed in place, and the same report went out every ~6 s
(750 sends in ~80 min) until the row was deleted by hand.

Unit coverage: `tests/workflow/test_a_delivered_event_is_never_resent.py`.

## Before deploy (local)

- [ ] `uv run pytest tests/workflow/test_a_delivered_event_is_never_resent.py` — 7 passed
- [ ] Wire-cut each guard (normalize · post-send try · `max_attempts`) — the matching tests go red, with all 7 still collected

## After deploy (prod)

- [ ] The next scheduled BStockReport run (Mon 2026-10-19 00:30 UTC) sends **one**
      Telegram message.
- [ ] Worker log has a `connector_delivery_dispatched` line for that deliverable, followed by
      `delivery_dispatched`, and **no** `delivery_worker_dispatch_failed`:
      `docker --context colima logs --since 30m bsvibe-prod-worker-1 | grep <deliverable_id>`
- [ ] `select count(*) from delivery_events;` is 0 after the tick.
- [ ] The run leaves `running` (auto-resolve after delivery ran) — `bsvibe_runs_show`.
- [ ] If a row ever fails repeatedly: at most `max_attempts` (5) `delivery_worker_dispatch_failed`
      lines, then one `delivery_worker_event_dropped` at error level.
