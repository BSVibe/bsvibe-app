"use client";

import { logout } from "@/lib/api/auth";
import { decodeClaims } from "@/lib/auth/claims";
import { useSession } from "@/lib/auth/session";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

/**
 * Settings → Account. Profile, Sign-in identities, Active sessions.
 *
 * Scope is frontend-first and HONEST: every datum the PWA already has is wired
 * REAL; surfaces that would need a backend that doesn't exist yet are HIDDEN
 * rather than shown as dead/disabled controls (L6 cleanup).
 *
 *  - Profile (REAL display): avatar initials, email, display name — all derived
 *    from the session + display-only JWT claims (`decodeClaims`, unverified —
 *    the backend verifies on every call). "Change photo" stays disabled (no
 *    Supabase profile write this lift).
 *  - Sign-in identities (REAL, read-only): the provider(s) actually present in
 *    the JWT render as "Signed in with X". No Connect/Disconnect controls —
 *    OAuth identity linking has no backend, so the dead affordances were removed.
 *  - Active sessions (REAL): the CURRENT session shows as "This device" with a
 *    REAL "Sign out" wired to the same `logout()` path AccountChip uses. There
 *    is no remote-session listing, so no "coming soon" note is shown.
 *
 *  Plan/billing is omitted entirely until billing is real.
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

      {/* ── Sign-in identities ──────────────────────────────────────────── */}
      {/* L6 §4 — read-only. Only the provider(s) actually present in the JWT are
          shown as "Signed in with X"; the disabled Connect/Disconnect buttons
          (no backend identity linking) were removed so no dead control implies a
          feature that doesn't exist. */}
      <section className="account-section" aria-label={t("signInIdentities")}>
        <h2 className="section-label">{t("signInIdentities")}</h2>
        <ul className="account-list">
          {IDENTITY_PROVIDER_KEYS.filter((key) => connected.has(key)).map((key) => (
            <li key={key} className="account-list__row">
              <span className="account-list__name">
                {t("signedInWith", { provider: t(`providers.${key}`) })}
              </span>
              <span className="account-pill account-pill--on">{t("connected")}</span>
            </li>
          ))}
        </ul>
      </section>

      {/* ── Active sessions ─────────────────────────────────────────────── */}
      {/* L6 §4 — the current-device sign-out is REAL and kept. The
          "other devices coming soon" note was removed (no remote-session
          listing backend). */}
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
      </section>
    </div>
  );
}
