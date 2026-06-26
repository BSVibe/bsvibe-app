"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import AccountChip from "./AccountChip";
import RailProducts from "./RailProducts";
import { BriefIcon, KnowledgeIcon, SettingsIcon, SkillsIcon } from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  knowledge: KnowledgeIcon,
  skills: SkillsIcon,
};

/** Persistent left rail (desktop). UX §1.1 / §3.4 layout. The product create
 *  action lives in the PRODUCTS section ("+ Product"); the Direct affordance is
 *  the omnipresent FAB (AppShell), not a rail button. */
export default function LeftRail() {
  const pathname = usePathname();
  const tNav = useTranslations("nav");
  const tShell = useTranslations("shell");

  return (
    <aside className="rail">
      <div className="rail__brand">
        <span className="rail__wordmark">{tShell("wordmark")}</span>
        <span className="rail__tagline">{tShell("tagline")}</span>
      </div>

      <nav className="rail__nav" aria-label={tShell("primaryNav")}>
        {PRIMARY_NAV.map((item) => {
          const Icon = ICONS[item.key];
          const active = pathname === item.href;
          if (!item.available) {
            return (
              <button
                key={item.key}
                type="button"
                className="rail__item"
                disabled
                title={tShell("comingSoon")}
              >
                <Icon />
                <span>{tNav(item.key)}</span>
              </button>
            );
          }
          return (
            <Link
              key={item.key}
              href={item.href}
              className="rail__item"
              aria-current={active ? "page" : undefined}
            >
              <Icon />
              <span>{tNav(item.key)}</span>
            </Link>
          );
        })}
      </nav>

      {/* PRODUCTS — a separate section (NOT a primary-nav entry): the
          workspace's products + a "+ Product" create flow. Kept self-contained
          in RailProducts so the nav list above stays untouched. */}
      <RailProducts />

      <div className="rail__foot">
        <Link
          href="/settings"
          className="rail__item rail__item--sub"
          aria-current={pathname === "/settings" ? "page" : undefined}
        >
          <SettingsIcon />
          <span>{tNav("settings")}</span>
        </Link>
        <AccountChip />
      </div>
    </aside>
  );
}
