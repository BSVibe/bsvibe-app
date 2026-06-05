"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { completeOAuth, getPendingOAuthProvider } from "@/lib/api/auth";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/** Social sign-in return URL. The provider redirected here with `?code=`; we
 *  exchange it (PKCE verifier from sessionStorage) and land on /brief. A missing
 *  code (derived at render) or a failed exchange shows a calm, recoverable
 *  error — never a blank spinner. */
export default function CallbackPage() {
  const router = useRouter();
  const params = useSearchParams();
  const t = useTranslations("auth");
  const code = params.get("code");
  const [failed, setFailed] = useState(false);
  const ran = useRef(false);

  useEffect(() => {
    if (!code || ran.current) return;
    ran.current = true;
    completeOAuth(getPendingOAuthProvider(), code)
      .then(() => {
        // Honour the `return_to` the login page stashed before the
        // social-provider hand-off so the OAuth consent flow lands back
        // on the consent screen instead of /brief.
        const target = readReturnTo();
        if (typeof window !== "undefined") {
          sessionStorage.removeItem("bsvibe.return_to");
        }
        router.replace(target);
      })
      .catch(() => setFailed(true));
  }, [code, router]);

  const showError = failed || !code;

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        <div className="login__status">
          {showError ? (
            <>
              <p className="login__error" role="alert">
                {t("oauthCallbackError")}
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

/** Resolve the same-origin `return_to` for the post-OAuth redirect.
 *
 *  Primary source: the ``#return_to=…`` HASH FRAGMENT the login page bakes
 *  into the IdP redirect_to URL via ``startOAuth``. Hash is used because
 *  Supabase's redirect URL allow-list is exact-match on path + query — a
 *  query param breaks the match and Supabase falls back to the Site URL.
 *  Hash fragments are never sent to the server, so the allow-list check
 *  passes; the browser preserves the fragment through the IdP 302 chain.
 *
 *  Fallback chain: legacy ``?return_to=…`` query param (still emitted by
 *  callers that haven't redeployed yet) → legacy ``bsvibe.return_to``
 *  sessionStorage key (for sessions started before the URL-encoded shape).
 *
 *  Rejects anything but a relative path so this page can't be turned
 *  into an open redirector. */
function readReturnTo(): string {
  if (typeof window === "undefined") return "/brief";
  const fromHash = new URLSearchParams(
    window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "",
  ).get("return_to");
  const fromUrl = new URLSearchParams(window.location.search).get("return_to");
  const raw = fromHash ?? fromUrl ?? sessionStorage.getItem("bsvibe.return_to");
  if (!raw) return "/brief";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/brief";
  return raw;
}
