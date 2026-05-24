"use client";

import { usePendingDecisionsCount } from "@/lib/decisions/pending-count";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ActivityIcon,
  BellIcon,
  BriefIcon,
  DecisionsIcon,
  KnowledgeIcon,
  SettingsIcon,
  SkillsIcon,
} from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  decisions: DecisionsIcon,
  activity: ActivityIcon,
  knowledge: KnowledgeIcon,
  skills: SkillsIcon,
};

/** Map a pathname to the `nav` namespace key used for the mobile title. */
const TITLE_KEYS: Record<string, NavKey | "settings"> = {
  "/brief": "brief",
  "/decisions": "decisions",
  "/activity": "activity",
  "/knowledge": "knowledge",
  "/skills": "skills",
  "/settings": "settings",
};

/** Mobile top bar — page title + notifications (UX Brief mobile mockup). */
export function MobileTopBar() {
  const pathname = usePathname();
  const tNav = useTranslations("nav");
  const tShell = useTranslations("shell");
  const titleKey = TITLE_KEYS[pathname];
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

/** Mobile bottom tab bar — Brief / Decisions / Inside. */
export function MobileNav() {
  const pathname = usePathname();
  const pendingDecisions = usePendingDecisionsCount();
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
        const badge = item.key === "decisions" && pendingDecisions > 0 ? pendingDecisions : null;
        return (
          <Link
            key={item.key}
            href={item.href}
            className="tabbar__item"
            aria-current={active ? "page" : undefined}
          >
            <span className="tabbar__icon">
              <Icon />
              {badge !== null && (
                <span className="tabbar__badge" aria-label={tShell("pending", { count: badge })}>
                  {badge}
                </span>
              )}
            </span>
            <span>{tNav(item.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
