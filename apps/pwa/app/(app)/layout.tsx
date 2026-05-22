import RequireAuth from "@/components/auth/RequireAuth";
import AppShell from "@/components/shell/AppShell";
import type { ReactNode } from "react";

/** Authed segment: every surface under here is gated and wrapped in the shell. */
export default function AppGroupLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
