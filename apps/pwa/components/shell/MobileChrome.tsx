"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BellIcon, BriefIcon, DecisionsIcon, InsideIcon } from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  decisions: DecisionsIcon,
  inside: InsideIcon,
};

const TITLES: Record<string, string> = { "/brief": "Brief" };

/** Mobile top bar — page title + notifications (UX Brief mobile mockup). */
export function MobileTopBar() {
  const pathname = usePathname();
  const title = TITLES[pathname] ?? "BSVibe";
  return (
    <header className="topbar">
      <span className="topbar__title">{title}</span>
      <button type="button" className="topbar__bell" disabled title="Notifications — coming soon">
        <BellIcon />
      </button>
    </header>
  );
}

/** Mobile bottom tab bar — Brief / Decisions / Inside. */
export function MobileNav() {
  const pathname = usePathname();
  return (
    <nav className="tabbar" aria-label="Primary">
      {PRIMARY_NAV.map((item) => {
        const Icon = ICONS[item.key];
        const active = pathname === item.href;
        if (!item.available) {
          return (
            <button key={item.key} type="button" className="tabbar__item" disabled>
              <Icon />
              <span>{item.label}</span>
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
            <Icon />
            <span>{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
