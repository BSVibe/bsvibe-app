"use client";

import { getNotificationPrefs, updateNotificationPrefs } from "@/lib/api/notifications";
import type { NotificationPrefs } from "@/lib/api/types";
import { useTranslations } from "next-intl";
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
 *  backend matrix is keyed on; the visible label + short name come from the
 *  `settings.notifications.events.<id>` catalog. */
const EVENT_IDS = ["needs_you", "triggered", "shipped", "failed", "daily_brief"] as const;

/** The three channels (matrix columns). Labels come from the catalog. */
const CHANNEL_IDS = ["in_app", "email", "slack"] as const;

type LoadState = { prefs: NotificationPrefs; failed: false } | { prefs: null; failed: true } | null;

export default function NotificationsTab() {
  const [state, setState] = useState<LoadState>(null);
  const [saveError, setSaveError] = useState(false);
  const t = useTranslations("settings.notifications");

  /** Short, human cell label for the accessible checkbox name, e.g.
   *  "toggle Slack for Daily Brief". */
  function cellLabel(eventId: string, channelId: string): string {
    return t("cellLabel", {
      channel: t(`channels.${channelId}`),
      event: t(`events.${eventId}.short`),
    });
  }

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
      <p className="general-tab__lede">{t("lede")}</p>

      {state === null ? (
        <p className="notifications__loading" aria-busy="true">
          {t("loading")}
        </p>
      ) : state.failed ? (
        <p className="notifications__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : (
        <>
          {saveError ? (
            <p className="notifications__note" aria-live="polite">
              {t("saveError")}
            </p>
          ) : null}

          <section className="notifications-matrix" aria-label={t("matrixLabel")}>
            <h2 className="section-label">{t("matrixHeading")}</h2>
            <table className="notifications-matrix__table">
              <thead>
                <tr>
                  <th scope="col" className="notifications-matrix__event-head">
                    {t("eventColumn")}
                  </th>
                  {CHANNEL_IDS.map((channelId) => (
                    <th key={channelId} scope="col" className="notifications-matrix__channel-head">
                      {t(`channels.${channelId}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {EVENT_IDS.map((eventId) => (
                  <tr key={eventId} className="notifications-matrix__row">
                    <th scope="row" className="notifications-matrix__event">
                      {t(`events.${eventId}.label`)}
                    </th>
                    {CHANNEL_IDS.map((channelId) => {
                      const on = state.prefs.matrix[eventId]?.[channelId] ?? false;
                      const label = cellLabel(eventId, channelId);
                      return (
                        <td key={channelId} className="notifications-matrix__cell">
                          <label className="toggle" aria-label={label}>
                            <input
                              type="checkbox"
                              className="toggle__input"
                              aria-label={label}
                              checked={on}
                              onChange={() => toggleCell(state.prefs, eventId, channelId)}
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
            <p className="notifications-matrix__caption">{t("matrixCaption")}</p>
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
  const t = useTranslations("settings.notifications");
  return (
    <section className="quiet-hours" aria-label={t("quietHours")}>
      <h2 className="section-label">{t("quietHours")}</h2>
      <div className="quiet-hours__row">
        <label className="quiet-hours__switch">
          <input
            type="checkbox"
            className="quiet-hours__switch-input"
            aria-label={t("quietHours")}
            checked={prefs.quiet_hours_enabled}
            onChange={() => onChange({ ...prefs, quiet_hours_enabled: !prefs.quiet_hours_enabled })}
          />
          <span>{t("quietHours")}</span>
        </label>

        <label className="quiet-hours__field">
          <span className="quiet-hours__field-label">{t("from")}</span>
          <input
            type="time"
            className="quiet-hours__time"
            aria-label={t("from")}
            value={prefs.quiet_hours_start}
            disabled={!prefs.quiet_hours_enabled}
            onChange={(e) => onChange({ ...prefs, quiet_hours_start: e.target.value })}
          />
        </label>

        <label className="quiet-hours__field">
          <span className="quiet-hours__field-label">{t("until")}</span>
          <input
            type="time"
            className="quiet-hours__time"
            aria-label={t("until")}
            value={prefs.quiet_hours_end}
            disabled={!prefs.quiet_hours_enabled}
            onChange={(e) => onChange({ ...prefs, quiet_hours_end: e.target.value })}
          />
        </label>
      </div>
      <p className="quiet-hours__caption">{t("quietHoursCaption")}</p>
    </section>
  );
}
