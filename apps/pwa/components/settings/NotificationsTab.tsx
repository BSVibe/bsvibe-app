"use client";

import { getNotificationPrefs, updateNotificationPrefs } from "@/lib/api/notifications";
import type { NotificationPrefsView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

/**
 * Settings → Notifications: one switch per event.
 *
 * Delivery is now wired (N2a/N3): needs_you / triggered / shipped / failed each
 * stage an outbox row that the NotifyWorker drains to the workspace's bound push
 * channels. So this surface is no longer the honest "coming soon" stub — it is a
 * live grid the founder can steer. The honesty rules baked in here:
 *
 *  - The CHANNEL AXIS IS GONE (2026-08-31). It never differentiated anything:
 *    measured on prod, one workspace carried columns for channels that had never
 *    been bound while LACKING the column for the one that was — so the founder
 *    bound Telegram and received nothing but `auth_down`, because the send path
 *    reads stored keys and an absent key meant "no". One switch per event removes
 *    the class of bug: there is no per-channel key that can be missing.
 *  - On means the in-app inbox AND every bound push channel. Noise is steered by
 *    WHICH connectors you bind, not by a grid.
 *  - `available_channels` is still shown — as a caption naming where an enabled
 *    event will actually land, so the switch never over-promises.
 *  - `daily_brief` is a LIVE row like the rest. It was rendered inert on the
 *    grounds that it "has NO producer yet"; measured in prod 2026-08-26 the
 *    DailyBriefWorker was running and `notification_events` held 76 daily_brief
 *    rows, ALL sent, the newest that same day — with the founder's own prefs
 *    enabling it on a push channel. The worker gates on those prefs, so the
 *    events themselves prove the toggle was ON while this grid drew it as an
 *    unchecked, disabled box. The founder could not switch off something they
 *    were receiving, and the UI told them they were not receiving it.
 *  - Zero push connectors ⇒ a connect-a-channel empty state (deep link to
 *    Connectors), not a bare in-app-only grid.
 *
 * Writes are optimistic with revert-on-failure (mirrors GeneralTab's
 * `chooseSafeMode`): flip the switch immediately, PUT the whole matrix + quiet
 * hours, reconcile from the response, revert on error.
 */

// Every one of them delivering, every one of them togglable.
const DELIVERING_EVENTS = [
  "needs_you",
  "triggered",
  "shipped",
  "failed",
  "daily_brief",
  // Platform health, not a run moment. Its push channels default ON for any
  // bound connector (backend `DEFAULT_ON_EVENTS`) — during this outage the
  // in-app inbox is part of what breaks, so opt-in-by-absence defeats it.
  "auth_down",
] as const;
const IN_APP = "in_app";

type EventId = (typeof DELIVERING_EVENTS)[number];

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

  function toggleEvent(event: EventId, on: boolean) {
    if (!prefs) return;
    commit({ ...prefs, matrix: { ...prefs.matrix, [event]: on } });
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
          {/* 켜면 어디로 가는지 이름을 밝힌다 — 스위치가 과약속하지 않도록. */}
          <p className="settings-field__caption">
            {t("deliversTo", {
              channels: [channelLabel(IN_APP), ...pushChannels.map(channelLabel)].join(", "),
            })}
          </p>
          <ul className="notifications-events">
            {DELIVERING_EVENTS.map((event) => (
              <li key={event} className="notifications-events__row">
                <label className="notifications-events__label">
                  <input
                    type="checkbox"
                    className="notifications-grid__toggle"
                    aria-label={eventLabel(event)}
                    checked={Boolean(prefs.matrix[event])}
                    disabled={saving}
                    onChange={(e) => toggleEvent(event, e.target.checked)}
                  />
                  <span>{eventLabel(event)}</span>
                </label>
              </li>
            ))}
          </ul>
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
