"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { type OAuthProvider, login, startOAuth } from "@/lib/api/auth";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

/** Sign in against the REAL backend. Email+password → `/api/auth/login`;
 *  social → `/api/auth/oauth/{provider}/authorize` (PKCE start, then the
 *  provider returns to `/auth/callback`). On success we land on /brief.
 *  Notion-craft centered card (UX §5): light surface, hairline card, social
 *  first, color reserved for the error line only. */
export default function LoginPage() {
  const router = useRouter();
  const t = useTranslations("auth");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [oauthBusy, setOauthBusy] = useState<OAuthProvider | null>(null);

  const disabled = busy || oauthBusy !== null;

  // Warm the /brief route (its chunk + RSC payload) while the founder is still
  // typing credentials, so the post-sign-in router.replace("/brief") paints
  // from cache instead of fetching on the critical path. Pairs with the
  // backend <link rel="preconnect"> (PR #170): connection warm + route warm.
  useEffect(() => {
    router.prefetch("/brief");
  }, [router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      router.replace("/brief");
    } catch {
      setError(t("signInError"));
      setBusy(false);
    }
  }

  async function handleOAuth(provider: OAuthProvider) {
    setError(null);
    setOauthBusy(provider);
    try {
      // On success this navigates away (window.location), so we never reset
      // oauthBusy on the happy path — the page is unmounting.
      await startOAuth(provider);
    } catch {
      setError(t("oauthError"));
      setOauthBusy(null);
    }
  }

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />

        <div className="login__head">
          <h1 className="login__title">{t("signInHeading")}</h1>
          <p className="login__subtitle">{t("signInSubtitle")}</p>
        </div>

        <div className="login__social">
          <button
            type="button"
            className="login__oauth"
            onClick={() => handleOAuth("google")}
            disabled={disabled}
          >
            <GoogleIcon />
            {t("continueWithGoogle")}
          </button>
          <button
            type="button"
            className="login__oauth"
            onClick={() => handleOAuth("github")}
            disabled={disabled}
          >
            <GitHubIcon />
            {t("continueWithGitHub")}
          </button>
        </div>

        <div className="login__divider">
          <span>{t("orContinueWithEmail")}</span>
        </div>

        <form className="login__form" onSubmit={handleSubmit}>
          <label className="login__label" htmlFor="email">
            {t("email")}
          </label>
          <input
            id="email"
            type="email"
            name="email"
            autoComplete="email"
            placeholder={t("emailPlaceholder")}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />

          <div className="login__label-row">
            <label className="login__label" htmlFor="password">
              {t("password")}
            </label>
            <Link className="login__forgot" href="/forgot-password">
              {t("forgotPassword")}
            </Link>
          </div>
          <div className="login__password">
            <input
              id="password"
              type={showPassword ? "text" : "password"}
              name="password"
              autoComplete="current-password"
              placeholder={t("passwordPlaceholder")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button
              type="button"
              className="login__eye"
              aria-label={showPassword ? t("hidePassword") : t("showPassword")}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((v) => !v)}
            >
              {showPassword ? <EyeOffIcon /> : <EyeIcon />}
            </button>
          </div>

          {error && (
            <p className="login__error" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="login__submit" disabled={disabled}>
            {busy ? t("signingIn") : t("continue")}
          </button>
        </form>
      </div>
    </main>
  );
}

function GoogleIcon() {
  return (
    <svg
      className="login__oauth-icon"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      aria-hidden="true"
    >
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"
      />
    </svg>
  );
}

function GitHubIcon() {
  return (
    <svg
      className="login__oauth-icon"
      width="18"
      height="18"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

function EyeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M3 3l18 18M10.6 10.6a3 3 0 0 0 4.24 4.24M9.9 5.2A9.5 9.5 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4M6.1 6.1A17 17 0 0 0 2 12s3.5 7 10 7a9.5 9.5 0 0 0 3-.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
