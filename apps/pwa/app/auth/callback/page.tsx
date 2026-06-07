"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import {
  RETURN_TO_KEY,
  completeOAuth,
  getPendingOAuthProvider,
  isSameOriginPath,
} from "@/lib/api/auth";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/** Social sign-in return URL. The provider redirected here with `?code=`; we
 *  exchange it (PKCE verifier from sessionStorage) and route the founder to
 *  the destination decided by {@link resolveReturnTo}. A missing code (derived
 *  at render) or a failed exchange shows a calm, recoverable error — never a
 *  blank spinner.
 *
 *  ## OAuth `return_to` round-trip — Lift E11 sequence
 *
 *  The PKCE-loopback `bsvibe login` flow needs the founder's browser to land
 *  back on the OAuth consent page after Supabase finishes social sign-in.
 *  The sequence:
 *
 *  ```
 *  CLI                 Backend             PWA                  Supabase
 *   │                    │                  │                      │
 *   │  open browser →    │                  │                      │
 *   │   /api/oauth/      │                  │                      │
 *   │    authorize       │                  │                      │
 *   │                    │ 302 → app.bsv... │                      │
 *   │                    │   /oauth/consent │                      │
 *   │                    │     ?<params>    │                      │
 *   │                    │                  │  no session →        │
 *   │                    │                  │   sessionStorage     │
 *   │                    │                  │   .setItem(          │
 *   │                    │                  │     return_to,       │
 *   │                    │                  │     consent_url)     │
 *   │                    │                  │  router.replace(     │
 *   │                    │                  │     /login)          │
 *   │                    │                  │  click Continue ─►   │
 *   │                    │                  │   startOAuth() also  │
 *   │                    │                  │   stashes return_to  │
 *   │                    │                  │   atomically before  │
 *   │                    │                  │   handing off to ───►│ Google
 *   │                    │                  │                      │
 *   │                    │                  │  ◄────────────── 302 │
 *   │                    │                  │  /auth/callback?code │
 *   │                    │                  │  completeOAuth       │
 *   │                    │                  │  resolveReturnTo() = │
 *   │                    │                  │  consent_url         │
 *   │                    │                  │  router.replace(     │
 *   │                    │                  │    consent_url)      │
 *   │                    │                  │  consent renders     │
 *   │                    │                  │   founder clicks     │
 *   │                    │                  │   Allow ────────────►│
 *   │                    │                  │  ◄────── redirect_to │
 *   │                    │                  │  =loopback?code=     │
 *   │  ◄─────────────── code captured ──────│                      │
 *   │  exchange code →   │                  │                      │
 *   │   /api/oauth/token │                  │                      │
 *   │  credentials saved │                  │                      │
 *  ```
 *
 *  The single load-bearing invariant: `RETURN_TO_KEY` is set by exactly two
 *  places — `ConsentClient` (when redirecting to /login) and `startOAuth`
 *  (atomically before `window.location.assign`). It is read + cleared by
 *  this file exactly once per round-trip. */
export default function CallbackPage() {
  const router = useRouter();
  const params = useSearchParams();
  const t = useTranslations("auth");
  const code = params.get("code");
  const [failed, setFailed] = useState<"exchange" | "lost_context" | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (!code || ran.current) return;
    ran.current = true;
    completeOAuth(getPendingOAuthProvider(), code)
      .then(() => {
        const resolved = resolveReturnTo();
        if (resolved === "lost_context") {
          setFailed("lost_context");
          return;
        }
        router.replace(resolved);
      })
      .catch(() => setFailed("exchange"));
  }, [code, router]);

  const showError = failed !== null || !code;

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        <div className="login__status">
          {showError ? (
            <>
              <p className="login__error" role="alert">
                {failed === "lost_context" ? t("oauthLostContext") : t("oauthCallbackError")}
              </p>
              <Link className="login__back" href="/login">
                {t("backToSignIn")}
              </Link>
            </>
          ) : (
            <p className="login__note">{t("completingSignIn")}</p>
          )}
        </div>
      </div>
    </main>
  );
}

/** Decide where to land after a successful code exchange.
 *
 *  Returns a same-origin path on success, or the sentinel `"lost_context"`
 *  when sessionStorage carried a value that failed the safety guard — which
 *  means an upstream caller wrote an unsafe value (a regression) or the
 *  sessionStorage entry was tampered with. Either way we surface a visible
 *  error rather than silently defaulting to `/brief` (which is the bug
 *  shape that hid the original Lift E4 PKCE-loopback failure).
 *
 *  The key is single-use — cleared after read so a subsequent vanilla
 *  sign-in cannot inherit the prior flow's destination. */
function resolveReturnTo(): string | "lost_context" {
  if (typeof window === "undefined") return "/brief";
  const stashed = sessionStorage.getItem(RETURN_TO_KEY);
  sessionStorage.removeItem(RETURN_TO_KEY);
  if (stashed === null) return "/brief";
  if (!isSameOriginPath(stashed)) return "lost_context";
  return stashed;
}
