/** Schedules API — REAL backend `/api/v1/schedules` (backend/api/v1/schedules.py):
 *  the authoring surface for `workspace_schedules`, the channel that lets BSVibe
 *  start work on its own. Before this the `ScheduleWorker` polled the table but
 *  nothing wrote rows — a dead channel; these are its producer.
 *
 *   POST   /api/v1/schedules        — author one schedule. Body is
 *                                      {@link ScheduleCreate}. The `text`
 *                                      instruction IS the scheduled run's task.
 *   GET    /api/v1/schedules        — list this workspace's schedules, newest first.
 *   DELETE /api/v1/schedules/{id}   — remove a schedule (204 No Content).
 *   PATCH  /api/v1/schedules/{id}   — enable / disable a schedule.
 *
 *  The backend accepts TWO kinds today — `instruction` and `product_tick`
 *  (`_SUPPORTED_KINDS` in `backend/schedule/application/schedule_service.py`;
 *  `product_tick` shipped 2026-07-21 in PR #609 and is live in prod). THIS UI
 *  authors `instruction` only. Read that as the UI's current scope, not as a
 *  claim about the capability: `product_tick` is authorable today through
 *  MCP/REST, and is absent here only because it takes no `text` and REQUIRES a
 *  `product_id` — a different form plus a product picker, a product decision
 *  still open in #948. `skill` / `plugin_action` genuinely do NOT exist (they
 *  are not in `_SUPPORTED_KINDS`). That split is machine-checked in
 *  `test/schedule-kinds-backend-parity.test.ts` so it cannot silently rot the
 *  way the sentence this one replaces did. Times (`next_run_at`) come back as
 *  UTC ISO strings; the surface formats them in the workspace time zone. */

import { apiFetch } from "./client";

/** The kind this UI authors — `payload={"text": <what to do>}`. NOT the only
 *  kind the backend accepts; see {@link BACKEND_SUPPORTED_SCHEDULE_KINDS}. */
export const SCHEDULE_KIND_INSTRUCTION = "instruction";

/** An autonomous cadence tick: the founder sets only WHEN (per product) and
 *  BSVibe decides WHAT at fire time. The backend accepts it; this UI does not
 *  author it yet (no product picker — #948), so it is created via MCP/REST. */
export const SCHEDULE_KIND_PRODUCT_TICK = "product_tick";

/** Every kind `POST /api/v1/schedules` accepts — a mirror of the backend's
 *  `_SUPPORTED_KINDS`, not an independent opinion about what exists. Pinned to
 *  the real frozenset by `test/schedule-kinds-backend-parity.test.ts`: adding a
 *  kind there without updating this goes red. */
export const BACKEND_SUPPORTED_SCHEDULE_KINDS = [
  SCHEDULE_KIND_INSTRUCTION,
  SCHEDULE_KIND_PRODUCT_TICK,
] as const;

/** Every kind THIS UI can author today — deliberately a SUBSET of the above.
 *  The gap is unbuilt UI, never an unbuilt capability; keeping the two lists
 *  apart is the whole of #948. */
export const PWA_AUTHORABLE_SCHEDULE_KINDS = [SCHEDULE_KIND_INSTRUCTION] as const;

/** A stored schedule row — mirrors the backend `ScheduleView`. `next_run_at` /
 *  `last_fired_at` are UTC ISO datetime strings. */
export interface Schedule {
  id: string;
  kind: string;
  text: string;
  cron_expr: string;
  product_id: string | null;
  title: string | null;
  next_run_at: string;
  last_fired_at: string | null;
  enabled: boolean;
}

/** Request body for authoring a schedule — mirrors the backend `ScheduleCreate`
 *  (`extra=forbid`). `kind` defaults to `instruction`, which is the only kind
 *  this UI sends today (see {@link PWA_AUTHORABLE_SCHEDULE_KINDS}); the field is
 *  open because the backend also takes `product_tick`. */
export interface ScheduleCreate {
  kind?: string;
  text: string;
  cron_expr: string;
  product_id?: string | null;
  title?: string | null;
}

/** List this workspace's schedules, newest first. */
export function getSchedules(): Promise<Schedule[]> {
  return apiFetch<Schedule[]>("/api/v1/schedules");
}

/** Author one schedule. 400 on an invalid cron expression / unsupported kind. */
export function createSchedule(body: ScheduleCreate): Promise<Schedule> {
  const payload: ScheduleCreate = {
    kind: body.kind ?? SCHEDULE_KIND_INSTRUCTION,
    text: body.text,
    cron_expr: body.cron_expr,
  };
  if (body.product_id != null) payload.product_id = body.product_id;
  if (body.title != null && body.title !== "") payload.title = body.title;
  return apiFetch<Schedule>("/api/v1/schedules", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Delete a schedule. 204 No Content, so this resolves to void. */
export function deleteSchedule(id: string): Promise<void> {
  return apiFetch<void>(`/api/v1/schedules/${id}`, { method: "DELETE" });
}

/** Enable or disable a schedule (PATCH). Returns the reconciled row. */
export function setScheduleEnabled(id: string, enabled: boolean): Promise<Schedule> {
  return apiFetch<Schedule>(`/api/v1/schedules/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
}
