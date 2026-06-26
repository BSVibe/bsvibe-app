"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { BellIcon, BriefIcon, KnowledgeIcon, SettingsIcon, SkillsIcon } from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  knowledge: KnowledgeIcon,
  skills: SkillsIcon,
};

/** Map a pathname's first segment to the `nav` namespace key used for the
 *  mobile title. Detail routes (``/deliverables/{id}``, ``/runs/{id}``,
 *  ``/products/{slug}``) get their own short label so the header keeps
 *  surface context instead of falling through to the BSVibe wordmark. */
const TITLE_KEYS: Record<string, NavKey | "settings" | "deliverable" | "run" | "product"> = {
  "/brief": "brief",
  "/knowledge": "knowledge",
  "/skills": "skills",
  "/settings": "settings",
  "/deliverables": "deliverable",
  "/runs": "run",
  "/products": "product",
};

/** Mobile top bar — page title + notifications (UX Brief mobile mockup). */
export function MobileTopBar() {
  const pathname = usePathname();
  const tNav = useTranslations("nav");
  const tShell = useTranslations("shell");
  // Match by the FIRST path segment so nested routes resolve too — e.g.
  // `/settings/general` (the redirect target of `/settings`) still reads
  // "Settings" instead of falling through to the "BSVibe" wordmark.
  const segment = `/${pathname.split("/")[1] ?? ""}`;
  const titleKey = TITLE_KEYS[segment];
  const title = titleKey ? tNav(titleKey) : tShell("wordmark");
  return (
    <header className="topbar">
      <span className="topbar__title">{title}</span>
      <div className="topbar__actions">
        <button
          type="button"
          className="topbar__bell"
          disabled
          title={tShell("notificationsComingSoon")}
        >
          <BellIcon />
        </button>
        {/* Mobile-only entry to Settings — the desktop left rail (which carries
            the Settings link) is hidden at this width. */}
        <Link
          href="/settings"
          className="topbar__settings"
          aria-current={pathname === "/settings" ? "page" : undefined}
          aria-label={tNav("settings")}
          title={tNav("settings")}
        >
          <SettingsIcon />
        </Link>
      </div>
    </header>
  );
}

/** Mobile bottom tab bar — Brief / Knowledge / Skills. */
export function MobileNav() {
  const pathname = usePathname();
  const tNav = useTranslations("nav");
  const tShell = useTranslations("shell");
  return (
    <nav className="tabbar" aria-label={tShell("primaryNav")}>
      {PRIMARY_NAV.map((item) => {
        const Icon = ICONS[item.key];
        const active = pathname === item.href;
        if (!item.available) {
          return (
            <button key={item.key} type="button" className="tabbar__item" disabled>
              <Icon />
              <span>{tNav(item.key)}</span>
            </button>
          );
        }
        return (
          <Link
            key={item.key}
            href={item.href}
            className="tabbar__item"
            aria-current={active ? "page" : undefined}
          >
            <span className="tabbar__icon">
              <Icon />
            </span>
            <span>{tNav(item.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
