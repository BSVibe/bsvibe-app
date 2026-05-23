/** Left-rail / bottom-nav information architecture (UX §4 IA lock). */

export type NavKey = "brief" | "decisions" | "activity" | "inside" | "skills";

export interface NavItem {
  key: NavKey;
  label: string;
  href: string;
  /** Every primary surface (Brief / Decisions / Activity / Inside) ships a real
   *  route. */
  available: boolean;
}

export const PRIMARY_NAV: NavItem[] = [
  { key: "brief", label: "Brief", href: "/brief", available: true },
  { key: "decisions", label: "Decisions", href: "/decisions", available: true },
  { key: "activity", label: "Activity", href: "/activity", available: true },
  { key: "inside", label: "Inside", href: "/inside", available: true },
  { key: "skills", label: "Skills", href: "/skills", available: true },
];
