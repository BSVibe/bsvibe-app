import type { useTranslations } from "next-intl";

type T = ReturnType<typeof useTranslations>;

/**
 * A calm, founder-friendly relative timestamp ("12m ago", "3d ago"). Falls
 * back to the raw ISO string if it can't be parsed. Locale-agnostic phrasing
 * comes from the i18n catalog (`decisions.time*`), so no `Intl.RelativeTimeFormat`
 * locale wiring is needed for this single surface.
 */
export function relativeTime(iso: string, t: T): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (minutes < 1) return t("timeJustNow");
  if (hours < 1) return t("timeMinutes", { n: minutes });
  if (days < 1) return t("timeHours", { n: hours });
  return t("timeDays", { n: days });
}
