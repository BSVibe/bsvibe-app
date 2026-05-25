/** Left-rail / bottom-nav information architecture (UX §4 IA lock). */

// Activity was merged into Brief (the "Work stream") — it is no longer a tab.
export type NavKey = "brief" | "decisions" | "knowledge" | "skills";

export interface NavItem {
  key: NavKey;
  label: string;
  href: string;
  /** Every primary surface (Brief / Decisions / Knowledge / Skills) ships a
   *  real route. */
  available: boolean;
}

export const PRIMARY_NAV: NavItem[] = [
  { key: "brief", label: "Brief", href: "/brief", available: true },
  { key: "decisions", label: "Decisions", href: "/decisions", available: true },
  { key: "knowledge", label: "Knowledge", href: "/knowledge", available: true },
  { key: "skills", label: "Skills", href: "/skills", available: true },
];
