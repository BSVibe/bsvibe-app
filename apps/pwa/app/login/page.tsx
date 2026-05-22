"use client";

import { login } from "@/lib/api/auth";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

/** Login against the REAL backend `/api/auth/login` → Supabase. On success
 *  the session is persisted and we land on /brief. */
export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      router.replace("/brief");
    } catch {
      setError("Couldn’t sign you in. Check your email and password.");
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <div className="login__brand">
        <span className="login__wordmark">BSVibe</span>
        <span className="login__tagline">AI Agent OS</span>
      </div>
      <form className="login__form" onSubmit={handleSubmit}>
        <label className="login__label" htmlFor="email">
          Email
        </label>
        <input
          id="email"
          type="email"
          name="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <label className="login__label" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          name="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && (
          <p className="login__error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="login__submit" disabled={busy}>
          {busy ? "Signing in…" : "Continue"}
        </button>
      </form>
    </main>
  );
}
