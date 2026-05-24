"use client";

import { ApiError } from "@/lib/api/client";
import { listDecisionsLog, listPendingProposals } from "@/lib/api/decisions";
import type { DecisionLogEntry, Proposal } from "@/lib/api/types";
import { setPendingDecisionsCount } from "@/lib/decisions/pending-count";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import DecisionDetail from "./DecisionDetail";
import ProposalRow from "./ProposalRow";
import ResolvedRow from "./ResolvedRow";

/**
 * The Decisions surface — the canonicalization proposals inbox (Stitch screens
 * 1175801d… Inbox + 5bf54bdf… detail). Two calm tabs over the SAME backend
 * queue (backend/api/v1/decisions.py):
 *
 *  - Pending   ← GET /api/v1/decisions?status_filter=pending  (proposals the
 *    founder must judge — Accept applies the linked merge, Reject leaves it)
 *  - Resolved  ← GET /api/v1/decisions/log  (the founder-approval audit trail)
 *
 * Selecting a pending item opens a focused detail/resolve panel. A client-side
 * search box filters the visible list. Each list degrades to empty on a per-
 * surface 4xx/network blip rather than blanking the page. Resolving an item
 * re-reads both queues so the surface reflects server state, and keeps the nav
 * pending-count badge in sync with the pending size only.
 *
 * Scope note: this lift is the canon proposals queue only. Paused-run
 * checkpoints (the other thing that used to live here) and Safe-Mode delivery
 * approvals stay in the Brief's "Needs you" — deliberately NOT mirrored here.
 */
type Tab = "pending" | "resolved";

export default function Decisions() {
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const [resolved, setResolved] = useState<DecisionLogEntry[] | null>(null);
  const [tab, setTab] = useState<Tab>("pending");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const t = useTranslations("decisions");

  const load = useCallback((onResult: (p: Proposal[], d: DecisionLogEntry[]) => void) => {
    Promise.all([
      listPendingProposals().catch(emptyOnApiError<Proposal>),
      listDecisionsLog().catch(emptyOnApiError<DecisionLogEntry>),
    ]).then(([p, d]) => {
      // Only PENDING proposals drive the nav badge — resolved items are history.
      setPendingDecisionsCount(p.length);
      onResult(p, d);
    });
  }, []);

  useEffect(() => {
    let active = true;
    load((p, d) => {
      if (!active) return;
      setProposals(p);
      setResolved(d);
    });
    return () => {
      active = false;
    };
  }, [load]);

  const reload = useCallback(() => {
    load((p, d) => {
      setProposals(p);
      setResolved(d);
      setSelectedId(null);
    });
  }, [load]);

  const filteredPending = useMemo(
    () => filterProposals(proposals ?? [], query),
    [proposals, query],
  );
  const filteredResolved = useMemo(() => filterDecisions(resolved ?? [], query), [resolved, query]);

  if (proposals === null || resolved === null) {
    return (
      <div className="decisions decisions--loading" aria-busy="true">
        <h1 className="decisions__heading">{t("heading")}</h1>
        <p className="decisions__loading-note">{t("loadingNote")}</p>
      </div>
    );
  }

  const selected = proposals.find((p) => p.id === selectedId) ?? null;

  return (
    <div className="decisions">
      <header className="decisions__masthead">
        <h1 className="decisions__heading">{t("heading")}</h1>
        <p className="decisions__lede">{t("lede")}</p>
      </header>

      <input
        type="search"
        className="decisions__search"
        aria-label={t("searchLabel")}
        placeholder={t("searchPlaceholder")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      <div className="decisions__tabs" role="tablist" aria-label={t("heading")}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "pending"}
          className="decisions__tab"
          onClick={() => setTab("pending")}
        >
          {t("tabPending")}
          <span className="decisions__tab-count">{proposals.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "resolved"}
          className="decisions__tab"
          onClick={() => setTab("resolved")}
        >
          {t("tabResolved")}
          <span className="decisions__tab-count">{resolved.length}</span>
        </button>
      </div>

      {tab === "pending" ? (
        filteredPending.length === 0 ? (
          <EmptyPending muted={query.length > 0} t={t} />
        ) : (
          <ul className="decisions-list" aria-label={t("tabPending")}>
            {filteredPending.map((item) => (
              <ProposalRow key={item.id} item={item} onOpen={() => setSelectedId(item.id)} />
            ))}
          </ul>
        )
      ) : filteredResolved.length === 0 ? (
        <section className="decisions-empty" aria-label={t("tabResolved")}>
          <p className="decisions-empty__line">{t("resolvedEmpty")}</p>
        </section>
      ) : (
        <ul className="decisions-list" aria-label={t("tabResolved")}>
          {filteredResolved.map((item) => (
            <ResolvedRow key={item.id} item={item} />
          ))}
        </ul>
      )}

      <p className="decisions__footnote">{t("footnote")}</p>

      {selected ? (
        <DecisionDetail item={selected} onClose={() => setSelectedId(null)} onResolved={reload} />
      ) : null}
    </div>
  );
}

function EmptyPending({
  muted,
  t,
}: {
  muted: boolean;
  t: ReturnType<typeof useTranslations>;
}) {
  return (
    <section className="decisions-empty" aria-label={t("tabPending")}>
      <p className="decisions-empty__line">{muted ? t("noMatches") : t("emptyLine")}</p>
      {muted ? null : <p className="decisions-empty__sub">{t("emptySub")}</p>}
    </section>
  );
}

/** Plain-language verb for a proposal (`merge-concepts` → `merge concepts`). */
export function proposalVerb(p: Proposal): string {
  return p.action_kind.replace(/-/g, " ");
}

/** Client-side filter over the proposal's verb + handle. */
function filterProposals(items: Proposal[], query: string): Proposal[] {
  const q = query.trim().toLowerCase();
  if (q.length === 0) return items;
  return items.filter((p) =>
    `${proposalVerb(p)} ${p.id} ${p.action_path} ${p.proposal_kind}`.toLowerCase().includes(q),
  );
}

/** Client-side filter over the resolved decision's kind + handle. */
function filterDecisions(items: DecisionLogEntry[], query: string): DecisionLogEntry[] {
  const q = query.trim().toLowerCase();
  if (q.length === 0) return items;
  return items.filter((d) => `${d.decision_kind} ${d.id}`.toLowerCase().includes(q));
}

/** Swallow a per-surface ApiError / network blip into an empty list so one
 *  failing queue does not blank the whole Decisions page. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}
