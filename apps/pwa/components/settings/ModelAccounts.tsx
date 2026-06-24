"use client";

import { createAccount, listAccounts, revokeAccount, setAccountActive } from "@/lib/api/accounts";
import type { ModelAccount } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import AddModelAccount from "./AddModelAccount";
import ModelAccountRow from "./ModelAccountRow";

/**
 * Settings → Model accounts. The founder registers / views / activates /
 * revokes the per-workspace LLM accounts the agent loop runs on. Backed by the
 * REAL /api/v1/accounts endpoints (backend/api/v1/accounts.py).
 *
 *  - List     ← GET /api/v1/accounts   (never the credential, only has_api_key)
 *  - Add      → POST /api/v1/accounts  (201 ModelAccountOut; secret never echoed)
 *  - Activate → PATCH /api/v1/accounts/{id} { is_active }
 *  - Revoke   → DELETE /api/v1/accounts/{id} (hard delete; confirm-gated)
 *
 * This section is load-bearing for the worker: the agent loop's model-account
 * resolution creates a needs-decision / pauses the run if there are zero (or
 * ambiguous) ACTIVE accounts. So the empty state is a gentle nudge rather than a
 * neutral "nothing here".
 *
 * The list loads on mount and re-reads after a successful create / toggle /
 * revoke so the section always reflects the server. A failed list read degrades
 * to a calm inline note rather than a blanked page; the Add form still works
 * (its own write is independent of the list's read).
 */
type ListState = { data: ModelAccount[]; failed: boolean } | null;

export default function ModelAccounts() {
  const [list, setList] = useState<ListState>(null);
  const [showAdd, setShowAdd] = useState(false);
  const t = useTranslations("settings.models");

  async function load() {
    try {
      setList({ data: await listAccounts(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listAccounts()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, []);

  const hasActive = list && !list.failed && list.data.some((a) => a.is_active);

  return (
    <section className="accounts" aria-label={t("sectionLabel")}>
      <header className="accounts__head">
        <h2 className="section-label">{t("sectionHeading")}</h2>
        {list && !list.failed && list.data.length > 0 ? (
          <span className="accounts__count">{list.data.length}</span>
        ) : null}
        {!showAdd && (
          <button type="button" className="settings-add-toggle" onClick={() => setShowAdd(true)}>
            {t("addToggle")}
          </button>
        )}
      </header>
      <p className="accounts__lede">{t("accountsLede")}</p>

      {list && !list.failed && list.data.length > 0 && !hasActive ? (
        <p className="accounts__warn" aria-live="polite">
          {t("noneActiveWarn")}
        </p>
      ) : null}

      {/* The add form is collapsed by default (progressive disclosure) so the
          section reads as the founder's accounts, not a wall of empty inputs.
          Opens on "+ Add account"; a successful create collapses it (the new
          row in the list is the confirmation). */}
      {showAdd && (
        <div className="settings-add-panel">
          <AddModelAccount
            onCreated={() => {
              load();
              setShowAdd(false);
            }}
            createAccount={createAccount}
          />
          <button type="button" className="settings-add-cancel" onClick={() => setShowAdd(false)}>
            {t("cancel")}
          </button>
        </div>
      )}

      {list === null ? (
        <p className="accounts__loading" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="accounts__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : list.data.length === 0 ? (
        <p className="accounts__empty">{t("empty")}</p>
      ) : (
        <ul className="accounts__list">
          {list.data.map((a) => (
            <ModelAccountRow
              key={a.id}
              account={a}
              onChanged={load}
              setActive={setAccountActive}
              revoke={revokeAccount}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
