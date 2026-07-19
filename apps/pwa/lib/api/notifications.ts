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
 *  Both return the `PrefsView` shape ({@link NotificationPrefsView}): the stored
 *  prefs PLUS `available_channels` (the workspace's live notify channels, derived
 *  per read from connector bindings). The PUT BODY is the narrower `PrefsBody`
 *  ({@link NotificationPrefs}) — matrix + quiet hours ONLY. `available_channels`
 *  is response-only, so `updateNotificationPrefs` sends just the body fields (the
 *  backend `extra=forbid` rejects an echoed `available_channels`). */

import { apiFetch } from "./client";
import type { NotificationPrefs, NotificationPrefsView } from "./types";

/** Notification preferences for the active workspace (get-or-create). */
export function getNotificationPrefs(): Promise<NotificationPrefsView> {
  return apiFetch<NotificationPrefsView>("/api/v1/notifications/prefs");
}

/** Replace the matrix + quiet hours wholesale. Sends only the writable
 *  `PrefsBody` fields (never `available_channels`) and returns the persisted
 *  prefs with the freshly-derived channels. */
export function updateNotificationPrefs(prefs: NotificationPrefs): Promise<NotificationPrefsView> {
  const body: NotificationPrefs = {
    matrix: prefs.matrix,
    quiet_hours_enabled: prefs.quiet_hours_enabled,
    quiet_hours_start: prefs.quiet_hours_start,
    quiet_hours_end: prefs.quiet_hours_end,
  };
  return apiFetch<NotificationPrefsView>("/api/v1/notifications/prefs", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
