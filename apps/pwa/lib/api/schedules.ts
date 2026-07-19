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
 *  S1 is the `instruction` kind ONLY — skill / product_tick / plugin_action are
 *  S4 and NOT built, so the UI never offers them. Times (`next_run_at`) come back
 *  as UTC ISO strings; the surface formats them in the workspace time zone. */

import { apiFetch } from "./client";

/** The only schedule kind that works today (S1). skill / product_tick /
 *  plugin_action are S4 — the UI must not offer them. */
export const SCHEDULE_KIND_INSTRUCTION = "instruction";

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
 *  (`extra=forbid`). `kind` defaults to `instruction`; the UI only ever sends
 *  that. */
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
