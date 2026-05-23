import SettingsShell from "@/components/settings/SettingsShell";
import type { ReactNode } from "react";

/** Settings segment: shared "Settings" header + 5-tab bar around each tab. */
export default function SettingsLayout({ children }: { children: ReactNode }) {
  return <SettingsShell>{children}</SettingsShell>;
}
