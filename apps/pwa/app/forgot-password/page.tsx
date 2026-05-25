"use client";

import { AuthBrand } from "@/components/auth/AuthBrand";
import { requestPasswordReset } from "@/lib/api/auth";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { type FormEvent, useState } from "react";

/** Request a password-recovery email. The backend always responds 204 (it never
 *  reveals whether the email exists), so we show the same "check your inbox"
 *  confirmation on success AND on error — no account-enumeration signal.
 *  Notion-craft centered card (UX §5). */
export default function ForgotPasswordPage() {
  const t = useTranslations("auth");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      await requestPasswordReset(email);
    } catch {
      // Leak-safe: a failure must not reveal anything — show the same state.
    }
    setSent(true);
  }

  return (
    <main className="login">
      <div className="login__card">
        <AuthBrand />
        {sent ? (
          <div className="login__status">
            <h1 className="login__title">{t("resetSentHeading")}</h1>
            <p className="login__note">{t("resetSentBody")}</p>
            <Link className="login__back" href="/login">
              {t("backToSignIn")}
            </Link>
          </div>
        ) : (
          <>
            <div className="login__head">
              <h1 className="login__title">{t("forgotHeading")}</h1>
              <p className="login__subtitle">{t("forgotSubtitle")}</p>
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
              <button type="submit" className="login__submit" disabled={busy}>
                {busy ? t("sending") : t("sendResetLink")}
              </button>
            </form>
            <Link className="login__back" href="/login">
              {t("backToSignIn")}
            </Link>
          </>
        )}
      </div>
    </main>
  );
}
