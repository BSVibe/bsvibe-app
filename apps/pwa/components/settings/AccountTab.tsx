"use client";

import { logout } from "@/lib/api/auth";
import { decodeClaims } from "@/lib/auth/claims";
import { useSession } from "@/lib/auth/session";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

/**
 * Settings → Account. Profile, Plan, Sign-in identities, Active sessions, per
 * the design (`~/Docs/bsvibe-deploy-verify/stitch/settings-account.png`).
 *
 * Scope is frontend-first: every datum the PWA already has is wired REAL, and
 * every control that would need a backend it doesn't have yet is present-but-
 * disabled with an honest "coming soon" hint — nothing is hidden.
 *
 *  - Profile (REAL display): avatar initials, email, display name — all derived
 *    from the session + display-only JWT claims (`decodeClaims`, unverified —
 *    the backend verifies on every call). "Change photo" + name editing are
 *    DISABLED: writing the profile needs a Supabase write this lift omits.
 *  - Plan (STUB): a clearly non-functional placeholder card. "Manage billing"
 *    is DISABLED — Stripe/billing is a separate backend, deferred.
 *  - Sign-in identities (PARTIAL real): the provider(s) in the JWT
 *    `app_metadata.providers` render as "Connected"; the other named providers
 *    show a DISABLED "Connect" — OAuth identity linking/unlinking needs a
 *    backend, deferred.
 *  - Active sessions (PARTIAL real): the CURRENT session shows as "This device"
 *    with a REAL "Sign out" wired to the same `logout()` path AccountChip uses.
 *    Listing OTHER devices + remote sign-out needs Supabase admin — deferred,
 *    surfaced as a disabled hint.
 */

/** The identity providers we surface, in the design's order. The `key` matches
 *  the lowercase value Supabase stores in `app_metadata.providers` (it maps
 *  "email" → password, which we don't list as a linkable identity here) and is
 *  also the `settings.account.providers` catalog key for the rendered label. */
const IDENTITY_PROVIDER_KEYS = ["google", "github", "apple"] as const;

/** Up to two initials for the avatar placeholder. Prefers a multi-word name
 *  ("Alex Chen" → "AC"); falls back to the first email/name character. */
function initialsFrom(name: string | null, email: string | null): string {
  const source = (name ?? email ?? "").trim();
  if (!source) return "?";
  const words = source.split(/\s+/).filter(Boolean);
  if (words.length >= 2) {
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  }
  return source.charAt(0).toUpperCase();
}

export default function AccountTab() {
  const session = useSession();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const t = useTranslations("settings.account");

  const claims = useMemo(() => decodeClaims(session?.accessToken), [session?.accessToken]);

  const email = session?.email ?? claims.email ?? t("notSignedIn");
  const displayName = claims.name ?? (session?.email ?? claims.email ?? "").split("@")[0] ?? "—";
  const initials = initialsFrom(claims.name, session?.email ?? claims.email);
  const connected = new Set(claims.providers.map((p) => p.toLowerCase()));

  async function handleSignOut() {
    setBusy(true);
    await logout();
    router.replace("/login");
  }

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      {/* ── Profile ─────────────────────────────────────────────────────── */}
      <section className="account-section" aria-label={t("profile")}>
        <h2 className="section-label">{t("profile")}</h2>
        <div className="account-profile">
          <div className="account-profile__photo">
            <span className="account-profile__avatar" aria-hidden="true">
              {initials}
            </span>
            <button type="button" className="account-link-btn" disabled title={t("comingSoon")}>
              {t("changePhoto")}
            </button>
          </div>
          <div className="account-profile__fields">
            <div className="account-kv">
              <span className="account-kv__label">{t("name")}</span>
              <span className="account-kv__value">{displayName}</span>
              <span className="account-kv__hint">{t("nameHint")}</span>
            </div>
            <div className="account-kv">
              <span className="account-kv__label">{t("email")}</span>
              <span className="account-kv__value">{email}</span>
            </div>
          </div>
        </div>
      </section>

      {/* ── Plan (placeholder) ──────────────────────────────────────────── */}
      <section className="account-section" aria-label={t("plan")}>
        <h2 className="section-label">{t("plan")}</h2>
        <div className="account-plan">
          <div className="account-plan__head">
            <div className="account-plan__name">
              <span className="account-plan__tier">{t("planTier")}</span>
              <span className="account-plan__price">{t("planPrice")}</span>
              <span className="account-plan__status">{t("planStatus")}</span>
            </div>
            <button type="button" className="account-btn" disabled title={t("comingSoon")}>
              {t("manageBilling")}
            </button>
          </div>
          <div className="account-plan__meta">
            <span className="account-plan__quota">{t("planQuota")}</span>
            <button type="button" className="account-link-btn" disabled title={t("comingSoon")}>
              {t("viewInvoices")}
            </button>
          </div>
        </div>
      </section>

      {/* ── Sign-in identities ──────────────────────────────────────────── */}
      <section className="account-section" aria-label={t("signInIdentities")}>
        <h2 className="section-label">{t("signInIdentities")}</h2>
        <ul className="account-list">
          {IDENTITY_PROVIDER_KEYS.map((key) => {
            const isConnected = connected.has(key);
            return (
              <li key={key} className="account-list__row">
                <span className="account-list__name">{t(`providers.${key}`)}</span>
                {isConnected ? (
                  <span className="account-list__actions">
                    <span className="account-pill account-pill--on">{t("connected")}</span>
                    <button
                      type="button"
                      className="account-link-btn"
                      disabled
                      title={t("comingSoon")}
                    >
                      {t("disconnect")}
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="account-link-btn"
                    disabled
                    title={t("comingSoon")}
                  >
                    {t("connect")}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
        <p className="account-note">{t("identitiesNote")}</p>
      </section>

      {/* ── Active sessions ─────────────────────────────────────────────── */}
      <section className="account-section" aria-label={t("activeSessions")}>
        <h2 className="section-label">{t("activeSessions")}</h2>
        <ul className="account-list">
          <li className="account-list__row">
            <span className="account-list__device">
              <span className="account-list__name">{t("thisDevice")}</span>
              <span className="account-list__sub">{t("activeNow", { email })}</span>
            </span>
            <button type="button" className="account-btn" onClick={handleSignOut} disabled={busy}>
              {busy ? t("signingOut") : t("signOut")}
            </button>
          </li>
        </ul>
        <p className="account-note">{t("sessionsNote")}</p>
      </section>
    </div>
  );
}
