"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import AccountChip from "./AccountChip";
import { BriefIcon, DecisionsIcon, InsideIcon, PlusIcon, SettingsIcon } from "./icons";
import { type NavKey, PRIMARY_NAV } from "./nav";

const ICONS: Record<NavKey, typeof BriefIcon> = {
  brief: BriefIcon,
  decisions: DecisionsIcon,
  inside: InsideIcon,
};

/** Persistent left rail (desktop). UX §1.1 / §3.4 layout. */
export default function LeftRail({ onDirect }: { onDirect: () => void }) {
  const pathname = usePathname();

  return (
    <aside className="rail">
      <div className="rail__brand">
        <span className="rail__wordmark">BSVibe</span>
        <span className="rail__tagline">AI Agent OS</span>
      </div>

      <nav className="rail__nav" aria-label="Primary">
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
                title="Coming soon"
              >
                <Icon />
                <span>{item.label}</span>
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
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <button type="button" className="rail__direct" onClick={onDirect}>
        <PlusIcon />
        <span>Direct</span>
      </button>

      <div className="rail__foot">
        <button type="button" className="rail__item rail__item--sub" disabled title="Coming soon">
          <SettingsIcon />
          <span>Settings</span>
        </button>
        <AccountChip />
      </div>
    </aside>
  );
}
