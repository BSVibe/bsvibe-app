/** Left-rail / bottom-nav information architecture (UX §4 IA lock). */

// Activity AND Decisions were both merged into Brief (the "Work stream"): a
// decision is now an inline state of a work-stream (the Brief's "Needs you"),
// not its own tab. Neither is a primary-nav entry any more.
export type NavKey = "brief" | "knowledge" | "skills";

export interface NavItem {
  key: NavKey;
  label: string;
  href: string;
  /** Every primary surface (Brief / Knowledge / Skills) ships a real route. */
  available: boolean;
}

export const PRIMARY_NAV: NavItem[] = [
  { key: "brief", label: "Brief", href: "/brief", available: true },
  { key: "knowledge", label: "Knowledge", href: "/knowledge", available: true },
  { key: "skills", label: "Skills", href: "/skills", available: true },
];
