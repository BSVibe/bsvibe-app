"use client";

import { logout } from "@/lib/api/auth";
import { decodeClaims } from "@/lib/auth/claims";
import { useSession } from "@/lib/auth/session";
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

/** The identity providers we surface, in the design's order. The label is what
 *  we render; `key` matches the lowercase value Supabase stores in
 *  `app_metadata.providers` (it maps "email" → password, which we don't list as
 *  a linkable identity here). */
const IDENTITY_PROVIDERS: { key: string; label: string }[] = [
  { key: "google", label: "Google" },
  { key: "github", label: "GitHub" },
  { key: "apple", label: "Apple" },
];

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

  const claims = useMemo(() => decodeClaims(session?.accessToken), [session?.accessToken]);

  const email = session?.email ?? claims.email ?? "Not signed in";
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
      <p className="general-tab__lede">Account — your profile and plan.</p>

      {/* ── Profile ─────────────────────────────────────────────────────── */}
      <section className="account-section" aria-label="Profile">
        <h2 className="section-label">Profile</h2>
        <div className="account-profile">
          <div className="account-profile__photo">
            <span className="account-profile__avatar" aria-hidden="true">
              {initials}
            </span>
            <button type="button" className="account-link-btn" disabled title="Coming soon">
              Change photo
            </button>
          </div>
          <div className="account-profile__fields">
            <div className="account-kv">
              <span className="account-kv__label">Name</span>
              <span className="account-kv__value">{displayName}</span>
              <span className="account-kv__hint">Editing your name is coming soon.</span>
            </div>
            <div className="account-kv">
              <span className="account-kv__label">Email</span>
              <span className="account-kv__value">{email}</span>
            </div>
          </div>
        </div>
      </section>

      {/* ── Plan (placeholder) ──────────────────────────────────────────── */}
      <section className="account-section" aria-label="Plan">
        <h2 className="section-label">Plan</h2>
        <div className="account-plan">
          <div className="account-plan__head">
            <div className="account-plan__name">
              <span className="account-plan__tier">Free</span>
              <span className="account-plan__price">$0/mo</span>
              <span className="account-plan__status">Active</span>
            </div>
            <button type="button" className="account-btn" disabled title="Coming soon">
              Manage billing
            </button>
          </div>
          <div className="account-plan__meta">
            <span className="account-plan__quota">
              Usage &amp; quota appear here once billing lands.
            </span>
            <button type="button" className="account-link-btn" disabled title="Coming soon">
              View invoices
            </button>
          </div>
        </div>
      </section>

      {/* ── Sign-in identities ──────────────────────────────────────────── */}
      <section className="account-section" aria-label="Sign-in identities">
        <h2 className="section-label">Sign-in identities</h2>
        <ul className="account-list">
          {IDENTITY_PROVIDERS.map((provider) => {
            const isConnected = connected.has(provider.key);
            return (
              <li key={provider.key} className="account-list__row">
                <span className="account-list__name">{provider.label}</span>
                {isConnected ? (
                  <span className="account-list__actions">
                    <span className="account-pill account-pill--on">Connected</span>
                    <button type="button" className="account-link-btn" disabled title="Coming soon">
                      Disconnect
                    </button>
                  </span>
                ) : (
                  <button type="button" className="account-link-btn" disabled title="Coming soon">
                    Connect
                  </button>
                )}
              </li>
            );
          })}
        </ul>
        <p className="account-note">Linking and unlinking sign-in methods is coming soon.</p>
      </section>

      {/* ── Active sessions ─────────────────────────────────────────────── */}
      <section className="account-section" aria-label="Active sessions">
        <h2 className="section-label">Active sessions</h2>
        <ul className="account-list">
          <li className="account-list__row">
            <span className="account-list__device">
              <span className="account-list__name">This device</span>
              <span className="account-list__sub">{email} · Active now</span>
            </span>
            <button type="button" className="account-btn" onClick={handleSignOut} disabled={busy}>
              {busy ? "Signing out…" : "Sign out"}
            </button>
          </li>
        </ul>
        <p className="account-note">Seeing and signing out other devices is coming soon.</p>
      </section>
    </div>
  );
}
