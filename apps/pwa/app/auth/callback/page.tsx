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
      .then(() => router.replace("/brief"))
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
