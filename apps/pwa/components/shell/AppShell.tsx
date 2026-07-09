"use client";

import { type ReactNode, useEffect, useState } from "react";
import { DirectFab, DirectOverlay } from "./DirectAction";
import LeftRail from "./LeftRail";
import LocaleSync from "./LocaleSync";
import { MobileNav, MobileTopBar } from "./MobileChrome";

/**
 * The one app shell (UX §1.1): persistent left rail on desktop, top bar +
 * bottom tabs on mobile, and the omnipresent Direct affordance (⌘K + FAB).
 */
export default function AppShell({ children }: { children: ReactNode }) {
  const [directOpen, setDirectOpen] = useState(false);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setDirectOpen((open) => !open);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="shell">
      <LocaleSync />
      <LeftRail />
      <MobileTopBar />
      <main className="shell__main">{children}</main>
      <MobileNav />
      <DirectFab onClick={() => setDirectOpen(true)} />
      <DirectOverlay open={directOpen} onClose={() => setDirectOpen(false)} />
    </div>
  );
}
