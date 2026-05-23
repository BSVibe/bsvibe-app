/** Notifications API — REAL backend `/api/v1/notifications/prefs`
 *  (backend/api/v1/notifications.py): the founder's notification preferences,
 *  an events × channels enable matrix plus a quiet-hours window.
 *
 *   GET  /api/v1/notifications/prefs  — get-or-create the prefs for the active
 *                                       workspace (a fresh workspace reads
 *                                       sensible defaults, then persists them)
 *   PUT  /api/v1/notifications/prefs  — replace the matrix + quiet hours
 *                                       wholesale
 *
 *  The PUT body mirrors the backend `PrefsBody` (extra=forbid) 1:1 — the full
 *  matrix + the quiet-hours fields. v1 stores PREFERENCES only; real email/Slack
 *  send is a later phase. */

import { apiFetch } from "./client";
import type { NotificationPrefs } from "./types";

/** Notification preferences for the active workspace (get-or-create). */
export function getNotificationPrefs(): Promise<NotificationPrefs> {
  return apiFetch<NotificationPrefs>("/api/v1/notifications/prefs");
}

/** Replace the matrix + quiet hours wholesale. Returns the persisted prefs. */
export function updateNotificationPrefs(prefs: NotificationPrefs): Promise<NotificationPrefs> {
  return apiFetch<NotificationPrefs>("/api/v1/notifications/prefs", {
    method: "PUT",
    body: JSON.stringify(prefs),
  });
}
