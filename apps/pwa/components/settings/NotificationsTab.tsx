"use client";

import { getNotificationPrefs, updateNotificationPrefs } from "@/lib/api/notifications";
import type { NotificationPrefsView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * Settings → Notifications (N2b): the real events × channels matrix.
 *
 * Delivery is now wired (N2a/N3): needs_you / triggered / shipped / failed each
 * stage an outbox row that the NotifyWorker drains to the workspace's bound push
 * channels. So this surface is no longer the honest "coming soon" stub — it is a
 * live grid the founder can steer. The honesty rules baked in here:
 *
 *  - Columns are DERIVED from `available_channels` (in_app + the workspace's bound
 *    notify connectors), never a hardcoded list — bind Telegram and a Telegram
 *    column appears; unbind it and the column is gone.
 *  - The `in_app` column is INFORMATIONAL, not a toggle. The NotifyWorker never
 *    sends in_app (a Decision already surfaces in the Brief / SSE inbox), so a
 *    switch there would pretend to gate an always-on inbox. It renders "always on".
 *  - `daily_brief` has NO producer yet (it needs the Schedule track), so its row
 *    is rendered disabled with a "requires scheduled runs" note — never a live
 *    toggle that looks active but delivers nothing.
 *  - Zero push connectors ⇒ a connect-a-channel empty state (deep link to
 *    Connectors), not a bare in-app-only grid.
 *
 * Writes are optimistic with revert-on-failure (mirrors GeneralTab's
 * `chooseSafeMode`): flip the cell immediately, PUT the whole matrix + quiet
 * hours, reconcile from the response, revert on error.
 */

// Matrix rows. The four delivering events are togglable; daily_brief is rendered
// but inert (no producer) — kept visible so the grid reads as complete/honest.
const DELIVERING_EVENTS = ["needs_you", "triggered", "shipped", "failed"] as const;
const IN_APP = "in_app";

type EventId = (typeof DELIVERING_EVENTS)[number] | "daily_brief";

export default function NotificationsTab() {
  const t = useTranslations("settings.notifications");
  const [prefs, setPrefs] = useState<NotificationPrefsView | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    getNotificationPrefs()
      .then((p) => {
        if (active) setPrefs(p);
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      });
    return () => {
      active = false;
    };
  }, []);

  // One optimistic write path: mutate a fresh copy, reflect it, PUT, reconcile,
  // revert on failure. Every cell/quiet-hours change goes through here.
  function commit(next: NotificationPrefsView) {
    if (!prefs) return;
    const previous = prefs;
    setPrefs(next);
    setSaving(true);
    updateNotificationPrefs(next)
      .then((saved) => setPrefs(saved))
      .catch(() => setPrefs(previous))
      .finally(() => setSaving(false));
  }

  function toggleCell(event: EventId, channel: string, on: boolean) {
    if (!prefs) return;
    const nextMatrix = { ...prefs.matrix };
    nextMatrix[event] = { ...(nextMatrix[event] ?? {}), [channel]: on };
    commit({ ...prefs, matrix: nextMatrix });
  }

  function setQuietEnabled(on: boolean) {
    if (!prefs) return;
    commit({ ...prefs, quiet_hours_enabled: on });
  }
  function setQuietBound(which: "start" | "end", value: string) {
    if (!prefs) return;
    commit({
      ...prefs,
      quiet_hours_start: which === "start" ? value : prefs.quiet_hours_start,
      quiet_hours_end: which === "end" ? value : prefs.quiet_hours_end,
    });
  }

  function channelLabel(channel: string): string {
    // Known channels get a friendly label; a stale/unknown key falls back to its
    // raw id so a since-removed connector never renders a blank column header.
    const key = `channel.${channel}`;
    return t.has(key) ? t(key) : channel;
  }
  function eventLabel(event: EventId): string {
    return t(`event.${event}`);
  }

  if (loadFailed) {
    return (
      <div className="general-tab">
        <p className="general-tab__lede">{t("lede")}</p>
        <p className="notifications__note" role="alert">
          {t("loadError")}
        </p>
      </div>
    );
  }

  if (!prefs) {
    return (
      <div className="general-tab">
        <p className="general-tab__lede">{t("lede")}</p>
        <p className="notifications__note">{t("loading")}</p>
      </div>
    );
  }

  const pushChannels = prefs.available_channels.filter((c) => c !== IN_APP);

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      {pushChannels.length === 0 ? (
        <section className="notifications-empty" aria-label={t("emptyTitle")}>
          <h2 className="section-label">{t("emptyTitle")}</h2>
          <p className="notifications__note">{t("emptyBody")}</p>
          <Link className="notifications-empty__cta" href="/settings/connectors">
            {t("emptyCta")}
          </Link>
        </section>
      ) : (
        <section className="notifications-matrix" aria-label={t("matrixTitle")}>
          <h2 className="section-label">{t("matrixTitle")}</h2>
          <p className="settings-field__caption">{t("matrixCaption")}</p>
          <div className="notifications-matrix__scroll">
            <table className="notifications-grid">
              <thead>
                <tr>
                  <th scope="col" className="notifications-grid__event-head">
                    {t("eventColumn")}
                  </th>
                  <th scope="col" className="notifications-grid__channel-head">
                    {channelLabel(IN_APP)}
                    <span className="notifications-grid__always">{t("inAppAlways")}</span>
                  </th>
                  {pushChannels.map((channel) => (
                    <th scope="col" key={channel} className="notifications-grid__channel-head">
                      {channelLabel(channel)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {DELIVERING_EVENTS.map((event) => (
                  <tr key={event}>
                    <th scope="row" className="notifications-grid__event">
                      {eventLabel(event)}
                    </th>
                    <td className="notifications-grid__cell notifications-grid__cell--inapp">
                      <span className="notifications-grid__dot" title={t("inAppTooltip")}>
                        {t("inAppOn")}
                      </span>
                    </td>
                    {pushChannels.map((channel) => (
                      <td key={channel} className="notifications-grid__cell">
                        <input
                          type="checkbox"
                          className="notifications-grid__toggle"
                          aria-label={`${eventLabel(event)} — ${channelLabel(channel)}`}
                          checked={Boolean(prefs.matrix[event]?.[channel])}
                          disabled={saving}
                          onChange={(e) => toggleCell(event, channel, e.target.checked)}
                        />
                      </td>
                    ))}
                  </tr>
                ))}
                {/* daily_brief — visible but inert until the Schedule track wires a
                    producer. Disabled toggles + a note keep it honest. */}
                <tr className="notifications-grid__row--pending">
                  <th scope="row" className="notifications-grid__event">
                    {eventLabel("daily_brief")}
                    <span className="notifications-grid__pending-note">
                      {t("dailyBriefPending")}
                    </span>
                  </th>
                  <td className="notifications-grid__cell notifications-grid__cell--inapp">
                    <span className="notifications-grid__dot notifications-grid__dot--off">
                      {t("inAppOff")}
                    </span>
                  </td>
                  {pushChannels.map((channel) => (
                    <td key={channel} className="notifications-grid__cell">
                      <input
                        type="checkbox"
                        className="notifications-grid__toggle"
                        aria-label={`${eventLabel("daily_brief")} — ${channelLabel(channel)}`}
                        checked={false}
                        disabled
                        onChange={() => undefined}
                      />
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
          <p className="settings-field__caption">{t("inAppCaption")}</p>
        </section>
      )}

      <section className="notifications-quiet" aria-label={t("quietTitle")}>
        <h2 className="section-label">{t("quietTitle")}</h2>
        <label className="notifications-quiet__enable">
          <input
            type="checkbox"
            aria-label={t("quietEnable")}
            checked={prefs.quiet_hours_enabled}
            disabled={saving}
            onChange={(e) => setQuietEnabled(e.target.checked)}
          />
          {t("quietEnable")}
        </label>
        <div className="notifications-quiet__range">
          <label className="notifications-quiet__time">
            <span className="settings-field__label">{t("quietStart")}</span>
            <input
              type="time"
              className="settings-field__input"
              value={prefs.quiet_hours_start}
              disabled={!prefs.quiet_hours_enabled || saving}
              onChange={(e) => setQuietBound("start", e.target.value)}
            />
          </label>
          <label className="notifications-quiet__time">
            <span className="settings-field__label">{t("quietEnd")}</span>
            <input
              type="time"
              className="settings-field__input"
              value={prefs.quiet_hours_end}
              disabled={!prefs.quiet_hours_enabled || saving}
              onChange={(e) => setQuietBound("end", e.target.value)}
            />
          </label>
        </div>
        <p className="settings-field__caption">{t("quietCaption")}</p>
      </section>
    </div>
  );
}
