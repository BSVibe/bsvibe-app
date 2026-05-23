import { useTranslations } from "next-intl";
import Link from "next/link";

/**
 * Settings top tab bar. This is the serialization point for the 5-tab Settings
 * IA: the `TABS` list enumerates every tab once, in order, so later lifts only
 * fill the per-tab content (the stub bodies) and never touch this nav.
 *
 * Tabs are real shareable routes under `/settings/*`. The active tab is passed
 * by the route segment's layout/page (the slug), not derived here, so the
 * component stays pure and trivially testable. Labels come from the
 * `settings.tabs` catalog, keyed by slug.
 */

export type SettingsTabSlug = "general" | "models" | "connectors" | "notifications" | "account";

export const TABS: SettingsTabSlug[] = [
  "general",
  "models",
  "connectors",
  "notifications",
  "account",
];

export default function SettingsTabs({ active }: { active: SettingsTabSlug }) {
  const t = useTranslations("settings.tabs");
  return (
    <nav className="settings-tabs" aria-label={t("sectionsLabel")}>
      {TABS.map((slug) => {
        const isActive = slug === active;
        return (
          <Link
            key={slug}
            href={`/settings/${slug}`}
            className="settings-tabs__tab"
            aria-current={isActive ? "page" : undefined}
          >
            {t(slug)}
          </Link>
        );
      })}
    </nav>
  );
}
