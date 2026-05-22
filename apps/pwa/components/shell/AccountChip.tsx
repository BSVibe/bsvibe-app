"use client";

import { logout } from "@/lib/api/auth";
import { useSession } from "@/lib/auth/session";
import { useRouter } from "next/navigation";
import { useState } from "react";

/** Account affordance at the foot of the rail: identity + sign out. */
export default function AccountChip() {
  const session = useSession();
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const email = session?.email ?? "Signed in";
  const initial = (session?.email ?? "?").trim().charAt(0).toUpperCase() || "?";

  async function handleSignOut() {
    setBusy(true);
    await logout();
    router.replace("/login");
  }

  return (
    <div className="account-chip">
      <span className="account-chip__avatar" aria-hidden="true">
        {initial}
      </span>
      <span className="account-chip__email" title={email}>
        {email}
      </span>
      <button
        type="button"
        className="account-chip__signout"
        onClick={handleSignOut}
        disabled={busy}
      >
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </div>
  );
}
