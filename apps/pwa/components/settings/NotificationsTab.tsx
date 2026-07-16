"use client";

import { useTranslations } from "next-intl";

/**
 * Settings → Notifications.
 *
 * Delivery is NOT wired yet. `backend/notifications/` stores the preference
 * matrix ONLY — there is no sender, channel adapter, dispatcher, or scheduler,
 * and the only in-app surface (the mobile bell in `shell/MobileChrome.tsx`) is
 * disabled. Nothing consumes the matrix to send anything on any channel.
 *
 * So this tab must not imply BSVibe notifies you. Until a real Notifier exists,
 * it shows an honest "coming soon" state instead of an events × channels matrix
 * + quiet-hours window whose toggles would silently do nothing and whose copy
 * ("pings you", "summary every morning", "In-app always works") promised a
 * delivery the product cannot perform.
 *
 * The prefs storage and the /api/v1/notifications/prefs endpoints stay in place
 * (harmless, dormant) for the later delivery phase; this surface simply stops
 * making the false promise, so it neither reads nor writes them.
 */
export default function NotificationsTab() {
  const t = useTranslations("settings.notifications");

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <section className="notifications-comingsoon" aria-label={t("comingSoonTitle")}>
        <span className="notifications-comingsoon__badge">{t("comingSoonBadge")}</span>
        <h2 className="section-label">{t("comingSoonTitle")}</h2>
        <p className="notifications__note">{t("comingSoonBody")}</p>
      </section>
    </div>
  );
}
