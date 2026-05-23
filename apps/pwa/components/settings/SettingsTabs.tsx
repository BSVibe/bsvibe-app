import Link from "next/link";

/**
 * Settings top tab bar. This is the serialization point for the 5-tab Settings
 * IA: the `TABS` list enumerates every tab once, in order, so later lifts only
 * fill the per-tab content (the stub bodies) and never touch this nav.
 *
 * Tabs are real shareable routes under `/settings/*`. The active tab is passed
 * by the route segment's layout/page (the slug), not derived here, so the
 * component stays pure and trivially testable.
 */

export type SettingsTabSlug = "general" | "models" | "connectors" | "notifications" | "account";

interface TabDef {
  slug: SettingsTabSlug;
  label: string;
}

export const TABS: TabDef[] = [
  { slug: "general", label: "General" },
  { slug: "models", label: "Models" },
  { slug: "connectors", label: "Connectors" },
  { slug: "notifications", label: "Notifications" },
  { slug: "account", label: "Account" },
];

export default function SettingsTabs({ active }: { active: SettingsTabSlug }) {
  return (
    <nav className="settings-tabs" aria-label="Settings sections">
      {TABS.map((tab) => {
        const isActive = tab.slug === active;
        return (
          <Link
            key={tab.slug}
            href={`/settings/${tab.slug}`}
            className="settings-tabs__tab"
            aria-current={isActive ? "page" : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
