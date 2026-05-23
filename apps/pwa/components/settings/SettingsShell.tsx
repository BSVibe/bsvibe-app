"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import SettingsTabs, { type SettingsTabSlug } from "./SettingsTabs";

/**
 * The Settings surface chrome: the "Settings" heading + the 5-tab bar, hosting
 * the active tab's content below. Used by the settings segment layout so every
 * tab shares one header/nav and only the body swaps per route.
 *
 * The active tab is derived from the pathname (`/settings/<slug>`), defaulting
 * to "general" — keeping per-tab pages content-only.
 */

const SLUGS: SettingsTabSlug[] = ["general", "models", "connectors", "notifications", "account"];

function activeSlug(pathname: string | null): SettingsTabSlug {
  const last = (pathname ?? "").split("/").filter(Boolean).pop();
  return SLUGS.includes(last as SettingsTabSlug) ? (last as SettingsTabSlug) : "general";
}

export default function SettingsShell({ children }: { children: ReactNode }) {
  const active = activeSlug(usePathname());
  return (
    <div className="settings">
      <h1 className="settings__heading">Settings</h1>
      <SettingsTabs active={active} />
      <div className="settings__body">{children}</div>
    </div>
  );
}
