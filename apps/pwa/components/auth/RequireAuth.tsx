"use client";

import { useHydrated, useSession } from "@/lib/auth/session";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";

/**
 * Auth gate for the app shell. Once hydrated on the client, an unauthenticated
 * caller is redirected to `/login`; the children render only for an
 * authenticated session. The redirect lives in an effect (a navigation side
 * effect, not state) and waits for `useHydrated()` so the server snapshot
 * (`null`) never triggers a spurious redirect.
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const hydrated = useHydrated();
  const router = useRouter();

  useEffect(() => {
    if (hydrated && !session) {
      router.replace("/login");
    }
  }, [hydrated, session, router]);

  if (!hydrated || !session) {
    return <div className="auth-splash" aria-hidden="true" />;
  }

  return <>{children}</>;
}
