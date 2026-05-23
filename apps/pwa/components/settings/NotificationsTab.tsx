"use client";

import { getNotificationPrefs, updateNotificationPrefs } from "@/lib/api/notifications";
import type { NotificationPrefs } from "@/lib/api/types";
import { useEffect, useState } from "react";

/**
 * Settings → Notifications. The founder chooses which moments reach them on
 * which channel (the events × channels matrix) and a quiet-hours window. Backed
 * by the REAL /api/v1/notifications/prefs endpoints
 * (backend/api/v1/notifications.py).
 *
 *  - Load   ← GET /api/v1/notifications/prefs   (get-or-create defaults)
 *  - Save   → PUT /api/v1/notifications/prefs   (replace; fired on each toggle /
 *             quiet-hours change — optimistic, with a calm revert on failure)
 *
 * The matrix loads on mount. A failed read degrades to a calm inline note
 * rather than a blanked page. Per-product overrides from the design are
 * intentionally OMITTED in v1 (global matrix + quiet hours only); real
 * email/Slack SENDING is a later phase — this surface only stores the
 * preferences.
 */

/** The five notification moments (matrix rows). Ids are the STABLE keys the
 *  backend matrix is keyed on; labels mirror the design copy. */
const EVENTS: { id: string; label: string }[] = [
  { id: "needs_you", label: "Needs you — a decision is waiting" },
  { id: "triggered", label: "Triggered — work woke up from outside" },
  { id: "shipped", label: "Shipped — a deliverable was verified" },
  { id: "failed", label: "Failed — verification failed and BSVibe gave up" },
  { id: "daily_brief", label: "Daily Brief — summary every morning" },
];

/** The three channels (matrix columns). */
const CHANNELS: { id: string; label: string }[] = [
  { id: "in_app", label: "In-app" },
  { id: "email", label: "Email" },
  { id: "slack", label: "Slack" },
];

/** Short, human cell label for the accessible checkbox name, e.g.
 *  "toggle Slack for Daily Brief". */
function cellLabel(eventLabel: string, channelLabel: string): string {
  const event = eventLabel.split(" — ")[0];
  return `toggle ${channelLabel} for ${event}`;
}

type LoadState = { prefs: NotificationPrefs; failed: false } | { prefs: null; failed: true } | null;

export default function NotificationsTab() {
  const [state, setState] = useState<LoadState>(null);
  const [saveError, setSaveError] = useState(false);

  useEffect(() => {
    let active = true;
    getNotificationPrefs()
      .then((prefs) => active && setState({ prefs, failed: false }))
      .catch(() => active && setState({ prefs: null, failed: true }));
    return () => {
      active = false;
    };
  }, []);

  /** Optimistically apply `next`, PUT it, and revert + flag on failure. */
  async function save(prev: NotificationPrefs, next: NotificationPrefs) {
    setSaveError(false);
    setState({ prefs: next, failed: false });
    try {
      const saved = await updateNotificationPrefs(next);
      setState({ prefs: saved, failed: false });
    } catch {
      setState({ prefs: prev, failed: false });
      setSaveError(true);
    }
  }

  function toggleCell(prefs: NotificationPrefs, eventId: string, channelId: string) {
    const next: NotificationPrefs = {
      ...prefs,
      matrix: {
        ...prefs.matrix,
        [eventId]: {
          ...prefs.matrix[eventId],
          [channelId]: !prefs.matrix[eventId]?.[channelId],
        },
      },
    };
    void save(prefs, next);
  }

  return (
    <div className="general-tab">
      <p className="general-tab__lede">Notifications — when and where BSVibe pings you.</p>

      {state === null ? (
        <p className="notifications__loading" aria-busy="true">
          Loading your notification settings…
        </p>
      ) : state.failed ? (
        <p className="notifications__note" aria-live="polite">
          Couldn&rsquo;t load your notification settings right now — try again in a moment.
        </p>
      ) : (
        <>
          {saveError ? (
            <p className="notifications__note" aria-live="polite">
              Couldn&rsquo;t save that change — it&rsquo;s been undone. Try again in a moment.
            </p>
          ) : null}

          <section className="notifications-matrix" aria-label="Events and channels">
            <h2 className="section-label">Events × Channels</h2>
            <table className="notifications-matrix__table">
              <thead>
                <tr>
                  <th scope="col" className="notifications-matrix__event-head">
                    Event
                  </th>
                  {CHANNELS.map((c) => (
                    <th key={c.id} scope="col" className="notifications-matrix__channel-head">
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {EVENTS.map((event) => (
                  <tr key={event.id} className="notifications-matrix__row">
                    <th scope="row" className="notifications-matrix__event">
                      {event.label}
                    </th>
                    {CHANNELS.map((channel) => {
                      const on = state.prefs.matrix[event.id]?.[channel.id] ?? false;
                      const label = cellLabel(event.label, channel.label);
                      return (
                        <td key={channel.id} className="notifications-matrix__cell">
                          <label className="toggle" aria-label={label}>
                            <input
                              type="checkbox"
                              className="toggle__input"
                              aria-label={label}
                              checked={on}
                              onChange={() => toggleCell(state.prefs, event.id, channel.id)}
                            />
                            <span className="toggle__track" aria-hidden="true">
                              <span className="toggle__thumb" />
                            </span>
                          </label>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="notifications-matrix__caption">
              Channels follow your Connectors. In-app always works.
            </p>
          </section>

          <QuietHours prefs={state.prefs} onChange={(next) => void save(state.prefs, next)} />
        </>
      )}
    </div>
  );
}

/** The quiet-hours on/off + From/Until window. Each change PUTs the full prefs
 *  (the parent's optimistic `save`). */
function QuietHours({
  prefs,
  onChange,
}: {
  prefs: NotificationPrefs;
  onChange: (next: NotificationPrefs) => void;
}) {
  return (
    <section className="quiet-hours" aria-label="Quiet hours">
      <h2 className="section-label">Quiet hours</h2>
      <div className="quiet-hours__row">
        <label className="quiet-hours__switch">
          <input
            type="checkbox"
            className="quiet-hours__switch-input"
            aria-label="Quiet hours"
            checked={prefs.quiet_hours_enabled}
            onChange={() => onChange({ ...prefs, quiet_hours_enabled: !prefs.quiet_hours_enabled })}
          />
          <span>Quiet hours</span>
        </label>

        <label className="quiet-hours__field">
          <span className="quiet-hours__field-label">From</span>
          <input
            type="time"
            className="quiet-hours__time"
            aria-label="From"
            value={prefs.quiet_hours_start}
            disabled={!prefs.quiet_hours_enabled}
            onChange={(e) => onChange({ ...prefs, quiet_hours_start: e.target.value })}
          />
        </label>

        <label className="quiet-hours__field">
          <span className="quiet-hours__field-label">Until</span>
          <input
            type="time"
            className="quiet-hours__time"
            aria-label="Until"
            value={prefs.quiet_hours_end}
            disabled={!prefs.quiet_hours_enabled}
            onChange={(e) => onChange({ ...prefs, quiet_hours_end: e.target.value })}
          />
        </label>
      </div>
      <p className="quiet-hours__caption">
        During quiet hours, only &ldquo;Needs you&rdquo; reaches you in-app. Everything else waits
        for morning.
      </p>
    </section>
  );
}
