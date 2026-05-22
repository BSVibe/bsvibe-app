/** Left-rail / bottom-nav information architecture (UX §4 IA lock). */

export type NavKey = "brief" | "decisions" | "inside";

export interface NavItem {
  key: NavKey;
  label: string;
  href: string;
  /** Brief is the only surface this track ships; the rest are IA placeholders. */
  available: boolean;
}

export const PRIMARY_NAV: NavItem[] = [
  { key: "brief", label: "Brief", href: "/brief", available: true },
  { key: "decisions", label: "Decisions", href: "/decisions", available: false },
  { key: "inside", label: "Inside", href: "/inside", available: false },
];
