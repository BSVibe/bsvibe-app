"use client";

import { getWorkspace } from "@/lib/api/workspace";
import { getSession } from "@/lib/auth/session";
import { resolveActiveLocale } from "@/lib/i18n/config";
import { getLocaleCookie, setLocaleCookie } from "@/lib/i18n/locale";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Keeps the UI-chrome locale consistent with the active workspace's language
 * (founder decision 2026-07 — `workspaces.language` is the source of truth for
 * BOTH server-rendered content AND chrome).
 *
 * Auth lives in client-side localStorage, so the server can't read the workspace
 * at request time — it only sees the `bsvibe.locale` cookie. This client effect
 * closes that gap: on app load (and whenever the shell remounts after a
 * workspace switch) it fetches the workspace language and, if the cookie the
 * server reads disagrees, mirrors the workspace language into the cookie and
 * refreshes so the next server render ships the matching catalog. Best-effort:
 * logged out → no-op; a fetch miss leaves the cookie untouched. Renders nothing.
 */
export default function LocaleSync() {
  const router = useRouter();

  useEffect(() => {
    if (!getSession()) return;
    let cancelled = false;
    getWorkspace()
      .then((ws) => {
        if (cancelled) return;
        const current = getLocaleCookie();
        const target = resolveActiveLocale({ workspaceLanguage: ws.language, cookie: current });
        if (target !== current) {
          setLocaleCookie(target);
          router.refresh();
        }
      })
      .catch(() => {
        // Best-effort: a workspace read miss must never break the shell; the
        // cookie / Accept-Language fallback still renders a usable locale.
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  return null;
}
