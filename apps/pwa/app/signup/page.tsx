"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { signUp } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

/** Create an account with email + password → `POST /api/auth/signup`.
 *
 *  ⚠️ This ships against a CLOSED door: email sign-up is disabled in the
 *  Supabase project, so the backend answers 403 and the refusal below is the
 *  DEFAULT path today, not an edge case. It is written to read as "not open
 *  yet, use social" rather than as a crash.
 *
 *  Two success shapes, deliberately NOT collapsed (PR #1004):
 *
 *    • live session      → signed in; go to the app
 *    • confirmation sent → NOT signed in; show "check your mail"
 *
 *  Treating the second as a sign-in would push someone into the app whose
 *  address is unproven and for whom the backend created no user row.
 *
 *  Notion-craft centered card (UX §5) — same skeleton as /login and
 *  /forgot-password so the three auth screens stay one family. */
export default function SignupPage() {
  const router = useRouter();
  const t = useTranslations("auth");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmationSent, setConfirmationSent] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const { confirmationRequired } = await signUp(email, password);
      if (confirmationRequired) {
        // NOT signed in — the address is unproven. Stay put and say so.
        setConfirmationSent(true);
        return;
      }
      router.replace("/brief");
    } catch (err) {
      // Branch on the STATUS, not on words in the message. The first version
      // matched /not available|403|signup/i — and "signup" appears in nearly
      // every error this endpoint produces, so almost everything read as
      // "sign-up isn't open". That is the backend's PR #1004 defect mirrored
      // here, and on the backend it fooled its own author.
      setError(t(signupErrorKey(err)));
    } finally {
      setBusy(false);
    }
  }

  if (confirmationSent) {
    return (
      <main className="login">
        <div className="login__card">
          <AuthBrand />
          <div className="login__status">
            <h1 className="login__title">{t("confirmSentHeading")}</h1>
            <p className="login__note">{t("confirmSentBody")}</p>
            <Link className="login__back" href="/login">
              {t("backToSignIn")}
            </Link>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />

        <div className="login__head">
          <h1 className="login__title">{t("signUpHeading")}</h1>
          <p className="login__subtitle">{t("signUpSubtitle")}</p>
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

          <label className="login__label" htmlFor="password">
            {t("password")}
          </label>
          <div className="login__password">
            <input
              id="password"
              type={showPassword ? "text" : "password"}
              name="password"
              // `new-password` (not `current-password`): tells the password
              // manager to OFFER one instead of filling the sign-in entry.
              autoComplete="new-password"
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
              {showPassword ? "🙈" : "👁"}
            </button>
          </div>

          {error && (
            <p className="login__error" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="login__submit" disabled={busy}>
            {busy ? t("creatingAccount") : t("createAccount")}
          </button>
        </form>

        <p className="login__note">
          {t("haveAccount")}{" "}
          <Link className="login__back" href="/login">
            {t("signIn")}
          </Link>
        </p>
      </div>
    </main>
  );
}

/** Which message a signup failure deserves.
 *
 *  403 is the ONLY closed door — the backend reserves it for GoTrue's
 *  `signup_disabled`. 429 is "try later" and 4xx is the caller's to fix;
 *  calling either of those a closed door tells the user a fixable problem is
 *  permanent, which is exactly what the first version did. */
function signupErrorKey(err: unknown): string {
  if (!(err instanceof ApiError)) return "signUpError";
  if (err.status === 403) return "signUpUnavailable";
  if (err.status === 429) return "signUpRateLimited";
  if (err.status >= 400 && err.status < 500) return "signUpRejected";
  return "signUpError";
}
