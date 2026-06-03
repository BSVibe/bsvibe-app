"use client";

import {
  type OAuthClient,
  createOAuthClient,
  deleteOAuthClient,
  listOAuthClients,
} from "@/lib/api/oauth-clients";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

/**
 * Settings → Developer. OAuth client management for the embedded
 * authorization server (Lift D1).
 *
 * Founder workflow:
 *  1. Click "Add client" → fill name + redirect URI(s) + scopes.
 *  2. Submit → the new `client_id` is shown, copy-pasteable.
 *  3. Paste `client_id` into the external app (e.g. Claude Code's MCP
 *     config); the OAuth flow then bounces the user through `/api/oauth/
 *     authorize` for the one-click consent.
 *
 * The list is paged-lite — D1 expects a handful of clients per founder
 * (Claude Code + IDE plugins + ad-hoc); we render the full list as
 * one column without pagination. Add it later if it stops fitting.
 */

const DEFAULT_SCOPES = ["mcp:read", "mcp:write"] as const;
const ALL_SCOPES = ["mcp:read", "mcp:write", "mcp:admin"] as const;

export default function DeveloperTab() {
  const t = useTranslations("settings.developer");
  const [clients, setClients] = useState<OAuthClient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const rows = await listOAuthClients();
      setClients(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="general-tab">
      <p className="general-tab__lede">{t("lede")}</p>

      <section className="account-section" aria-label={t("clients.title")}>
        <header className="developer-tab__header">
          <h2 className="section-label">{t("clients.title")}</h2>
          <button
            type="button"
            className="developer-tab__add"
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? t("clients.cancel") : t("clients.add")}
          </button>
        </header>

        {showForm && (
          <CreateClientForm
            onCreated={async () => {
              setShowForm(false);
              await refresh();
            }}
          />
        )}

        {error && <p className="developer-tab__error">{error}</p>}

        {clients === null ? (
          <p className="developer-tab__hint">{t("loading")}</p>
        ) : clients.length === 0 ? (
          <p className="developer-tab__hint">{t("clients.empty")}</p>
        ) : (
          <ul className="developer-tab__list">
            {clients.map((c) => (
              <ClientRow key={c.client_id} client={c} onRevoke={refresh} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

interface CreateClientFormProps {
  onCreated: () => void | Promise<void>;
}

function CreateClientForm({ onCreated }: CreateClientFormProps) {
  const t = useTranslations("settings.developer");
  const [name, setName] = useState("");
  const [redirect, setRedirect] = useState("http://127.0.0.1/callback");
  const [scopes, setScopes] = useState<Set<string>>(new Set(DEFAULT_SCOPES));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<OAuthClient | null>(null);

  function toggleScope(s: string) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(s)) {
        next.delete(s);
      } else {
        next.add(s);
      }
      return next;
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const row = await createOAuthClient({
        client_name: name,
        redirect_uris: redirect
          .split(/\s+/)
          .map((s) => s.trim())
          .filter(Boolean),
        allowed_scopes: Array.from(scopes),
      });
      setCreated(row);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <div className="developer-tab__created">
        <p className="developer-tab__created-title">{t("clients.created")}</p>
        <p className="developer-tab__created-hint">{t("clients.createdHint")}</p>
        <div className="developer-tab__copy">
          <code>{created.client_id}</code>
        </div>
        <button
          type="button"
          className="developer-tab__add"
          onClick={() => {
            setCreated(null);
            void onCreated();
          }}
        >
          {t("clients.done")}
        </button>
      </div>
    );
  }

  return (
    <form className="developer-tab__form" onSubmit={handleSubmit}>
      <label className="developer-tab__label">
        <span>{t("clients.nameLabel")}</span>
        <input
          type="text"
          required
          maxLength={120}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Claude Code"
        />
      </label>
      <label className="developer-tab__label">
        <span>{t("clients.redirectLabel")}</span>
        <textarea
          required
          rows={2}
          value={redirect}
          onChange={(e) => setRedirect(e.target.value)}
          placeholder="http://127.0.0.1/callback"
        />
        <span className="developer-tab__hint-sm">{t("clients.redirectHint")}</span>
      </label>
      <fieldset className="developer-tab__scopes">
        <legend>{t("clients.scopesLabel")}</legend>
        {ALL_SCOPES.map((s) => (
          <label key={s} className="developer-tab__scope">
            <input type="checkbox" checked={scopes.has(s)} onChange={() => toggleScope(s)} />
            <code>{s}</code>
          </label>
        ))}
      </fieldset>
      {error && <p className="developer-tab__error">{error}</p>}
      <button
        type="submit"
        className="developer-tab__submit"
        disabled={busy || name.trim().length === 0}
      >
        {busy ? t("clients.submitting") : t("clients.submit")}
      </button>
    </form>
  );
}

interface ClientRowProps {
  client: OAuthClient;
  onRevoke: () => void | Promise<void>;
}

function ClientRow({ client, onRevoke }: ClientRowProps) {
  const t = useTranslations("settings.developer");
  const [busy, setBusy] = useState(false);

  async function handleRevoke() {
    setBusy(true);
    try {
      await deleteOAuthClient(client.client_id);
      await onRevoke();
    } finally {
      setBusy(false);
    }
  }

  const isRevoked = client.revoked_at !== null;
  return (
    <li className="developer-tab__row" data-revoked={isRevoked || undefined}>
      <div className="developer-tab__row-main">
        <span className="developer-tab__row-name">{client.client_name}</span>
        <code className="developer-tab__row-id">{client.client_id}</code>
      </div>
      <div className="developer-tab__row-meta">
        <span>
          {t("clients.scopes")}: {client.allowed_scopes.join(", ")}
        </span>
        <span>
          {t("clients.redirects")}: {client.redirect_uris.join(", ")}
        </span>
      </div>
      {isRevoked ? (
        <span className="developer-tab__revoked">{t("clients.revoked")}</span>
      ) : (
        <button
          type="button"
          className="developer-tab__revoke"
          onClick={handleRevoke}
          disabled={busy}
        >
          {busy ? t("clients.revoking") : t("clients.revoke")}
        </button>
      )}
    </li>
  );
}
