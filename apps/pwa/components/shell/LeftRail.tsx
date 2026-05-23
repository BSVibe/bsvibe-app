"use client";

import { usePendingDecisionsCount } from "@/lib/decisions/pending-count";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import AccountChip from "./AccountChip";
import {
  ActivityIcon,
  BriefIcon,
  DecisionsIcon,
  InsideIcon,
  PlusIcon,
  SettingsIcon,
  SkillsIcon,
} from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  decisions: DecisionsIcon,
  activity: ActivityIcon,
  inside: InsideIcon,
  skills: SkillsIcon,
};

/** Persistent left rail (desktop). UX §1.1 / §3.4 layout. */
export default function LeftRail({ onDirect }: { onDirect: () => void }) {
  const pathname = usePathname();
  const pendingDecisions = usePendingDecisionsCount();
  const tNav = useTranslations("nav");
  const tShell = useTranslations("shell");
  const tDirect = useTranslations("direct");

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
          const badge = item.key === "decisions" && pendingDecisions > 0 ? pendingDecisions : null;
          return (
            <Link
              key={item.key}
              href={item.href}
              className="rail__item"
              aria-current={active ? "page" : undefined}
            >
              <Icon />
              <span>{tNav(item.key)}</span>
              {badge !== null && (
                <span className="rail__badge" aria-label={tShell("pending", { count: badge })}>
                  {badge}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      <button type="button" className="rail__direct" onClick={onDirect}>
        <PlusIcon />
        <span>{tDirect("label")}</span>
      </button>

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
