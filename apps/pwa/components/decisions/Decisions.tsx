"use client";

import { listPendingDecisions } from "@/lib/api/pending";
import { listResolvedDecisions } from "@/lib/api/resolved";
import type { PendingDecision, Proposal, ResolvedDecision } from "@/lib/api/types";
import { useSession } from "@/lib/auth/session";
import { setPendingDecisionsCount } from "@/lib/decisions/pending-count";
import { useEventStream } from "@/lib/live-events/use-event-stream";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import CheckpointRow from "./CheckpointRow";
import DecisionDetail from "./DecisionDetail";
import DeliveryRow from "./DeliveryRow";
import DeliveryRunGroupRow from "./DeliveryRunGroupRow";
import ProposalRow from "./ProposalRow";
import ResolvedRow from "./ResolvedRow";

/**
 * The Decisions surface — the SINGLE place for everything that genuinely needs
 * the founder's judgment. Two calm tabs:
 *
 *  - Pending   ← three EXISTING backend queues aggregated client-side
 *    (lib/api/pending.ts), each row labeled by kind and wired to its OWN
 *    resolve endpoint, with no change to any endpoint's behaviour:
 *      · "delivery"  Safe-Mode held delivery — Approve / Decline inline
 *        (POST /api/v1/safemode/{id}/approve|deny)
 *      · "decision"  paused-run checkpoint — answer + resume inline
 *        (POST /api/v1/checkpoints/{id}/resolve)
 *      · "knowledge" canon proposal — opens the focused Accept / Reject panel
 *        (POST /api/v1/decisions/{path}/accept|reject)
 *  - Resolved  ← the SAME three queues' settled items, aggregated client-side
 *    (lib/api/resolved.ts): decided Safe-Mode deliveries + answered checkpoints
 *    + the canon decision log — so an item the founder acted on stays visible as
 *    history instead of vanishing.
 *
 * The Pending count = deliveries + checkpoints + proposals. Deliveries +
 * proposals are the SAME set the Brief "Needs you" strip shows, so the Pending
 * count matches the Brief for that overlap; paused-run checkpoints are folded
 * in here too (the Brief does not yet show them — Decisions is a superset by
 * exactly the pending-checkpoint count, the one kind the Brief omits).
 *
 * A client-side search box filters the visible list. Each queue degrades to
 * empty on a per-surface 4xx / network blip rather than blanking the page.
 * Resolving any item re-reads both tabs so the surface reflects server state,
 * and keeps the nav pending-count badge in sync with the full pending size.
 */
type Tab = "pending" | "resolved";

export default function Decisions() {
  const [pending, setPending] = useState<PendingDecision[] | null>(null);
  const [resolved, setResolved] = useState<ResolvedDecision[] | null>(null);
  const [tab, setTab] = useState<Tab>("pending");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const t = useTranslations("decisions");

  const load = useCallback((onResult: (p: PendingDecision[], d: ResolvedDecision[]) => void) => {
    Promise.all([
      // Both aggregators already degrade each per-surface failure to empty.
      listPendingDecisions().catch(() => [] as PendingDecision[]),
      listResolvedDecisions().catch(() => [] as ResolvedDecision[]),
    ]).then(([p, d]) => {
      // The whole pending list (all three kinds) drives the nav badge — resolved
      // items are history and never inflate it.
      setPendingDecisionsCount(p.length);
      onResult(p, d);
    });
  }, []);

  useEffect(() => {
    let active = true;
    load((p, d) => {
      if (!active) return;
      setPending(p);
      setResolved(d);
    });
    return () => {
      active = false;
    };
  }, [load]);

  const reload = useCallback(() => {
    load((p, d) => {
      setPending(p);
      setResolved(d);
      setSelectedId(null);
    });
  }, [load]);

  // B16 — wake up on backend live events so the queue stays current without
  // a manual refresh. The pending count + Resolved tab both reflect changes
  // the founder didn't trigger from this tab (e.g. a run hit needs_decision
  // server-side, or a Safe-Mode item was queued).
  const session = useSession();
  const liveRefresh = useCallback(() => {
    load((p, d) => {
      setPending(p);
      setResolved(d);
    });
  }, [load]);
  useEventStream({
    token: session?.accessToken ?? null,
    onDecisionPending: liveRefresh,
    onRunTerminal: liveRefresh,
    onDeliveryQueued: liveRefresh,
  });

  const filteredPending = useMemo(() => filterPending(pending ?? [], query), [pending, query]);
  const filteredResolved = useMemo(() => filterDecisions(resolved ?? [], query), [resolved, query]);

  if (pending === null || resolved === null) {
    return (
      <div className="decisions decisions--loading" aria-busy="true">
        <h1 className="decisions__heading">{t("heading")}</h1>
        <p className="decisions__loading-note">{t("loadingNote")}</p>
      </div>
    );
  }

  // The open detail panel only applies to the "knowledge" (proposal) kind.
  const selected = pending.find((p) => p.kind === "knowledge" && p.proposal.id === selectedId) as
    | Extract<PendingDecision, { kind: "knowledge" }>
    | undefined;

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
          <span className="decisions__tab-count">{pending.length}</span>
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
          <ul className="needs-list" aria-label={t("tabPending")}>
            {renderPendingWithRunGroups(filteredPending, {
              onOpen: (id) => setSelectedId(id),
              onResolved: reload,
            })}
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
        <DecisionDetail
          item={selected.proposal}
          onClose={() => setSelectedId(null)}
          onResolved={reload}
        />
      ) : null}
    </div>
  );
}

/** Render one Pending row by kind — delivery + decision resolve inline, a
 *  knowledge proposal opens the focused detail/resolve panel. */
function PendingItem({
  item,
  onOpen,
  onResolved,
}: {
  item: PendingDecision;
  onOpen: () => void;
  onResolved: () => void;
}) {
  switch (item.kind) {
    case "delivery":
      return <DeliveryRow item={item} onResolved={onResolved} />;
    case "decision":
      return <CheckpointRow item={item} onResolved={onResolved} />;
    default:
      return <ProposalRow item={item.proposal} onOpen={onOpen} />;
  }
}

/** The proposal id for a knowledge row (the detail-panel selection key); "" for
 *  the inline-resolving kinds, which never open the panel. */
function proposalId(item: PendingDecision): string {
  return item.kind === "knowledge" ? item.proposal.id : "";
}

/** B12a — render the unified Pending list with per-Run delivery groups.
 *
 *  Each "delivery" row carries a `runId` (Workflow §1.2 — Safe Mode is the
 *  per-Run transactional container). When ≥2 delivery rows share the same
 *  `runId`, insert a single grouped row ABOVE the first occurrence with an
 *  "Approve all (N)" action that approves every item of that run together
 *  (POST /api/v1/safemode/runs/{runId}/approve). The per-item rows still
 *  render normally below the group, so per-item Approve / Decline keeps
 *  working unchanged (back-compat). Legacy items with no `runId` get no
 *  group header — they render as today.
 */
function renderPendingWithRunGroups(
  items: PendingDecision[],
  handlers: { onOpen: (proposalId: string) => void; onResolved: () => void },
): ReactNode[] {
  // Count delivery rows per runId so we know which runs are multi-artifact.
  const countsByRun = new Map<string, number>();
  for (const item of items) {
    if (item.kind === "delivery" && item.runId) {
      countsByRun.set(item.runId, (countsByRun.get(item.runId) ?? 0) + 1);
    }
  }
  // Track which run groups have already been surfaced (so we render the
  // group header exactly once per run, ABOVE its first delivery row).
  const renderedGroupFor = new Set<string>();
  const nodes: ReactNode[] = [];
  for (const item of items) {
    if (
      item.kind === "delivery" &&
      item.runId &&
      (countsByRun.get(item.runId) ?? 0) >= 2 &&
      !renderedGroupFor.has(item.runId)
    ) {
      renderedGroupFor.add(item.runId);
      const groupItems = items.filter(
        (i): i is PendingDecision & { kind: "delivery" } =>
          i.kind === "delivery" && i.runId === item.runId,
      );
      nodes.push(
        <DeliveryRunGroupRow
          key={`run-group-${item.runId}`}
          runId={item.runId}
          items={groupItems}
          onResolved={handlers.onResolved}
        />,
      );
    }
    nodes.push(
      <PendingItem
        key={item.id}
        item={item}
        onOpen={() => handlers.onOpen(proposalId(item))}
        onResolved={handlers.onResolved}
      />,
    );
  }
  return nodes;
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

/** Searchable text for a Pending item across all three kinds. */
function pendingHaystack(item: PendingDecision): string {
  switch (item.kind) {
    case "delivery":
      return `delivery ${item.itemId}`;
    case "decision":
      return `decision ${item.question} ${item.rationale ?? ""} ${item.checkpointId}`;
    default: {
      const p = item.proposal;
      return `knowledge ${proposalVerb(p)} ${p.id} ${p.action_path} ${p.proposal_kind}`;
    }
  }
}

/** Client-side filter over the unified Pending list. */
function filterPending(items: PendingDecision[], query: string): PendingDecision[] {
  const q = query.trim().toLowerCase();
  if (q.length === 0) return items;
  return items.filter((item) => pendingHaystack(item).toLowerCase().includes(q));
}

/** Searchable text for a Resolved item across all three kinds. */
function resolvedHaystack(item: ResolvedDecision): string {
  switch (item.kind) {
    case "delivery":
      return `delivery ${item.status} ${item.itemId}`;
    case "decision":
      return `decision ${item.question} ${item.resolution ?? ""} ${item.checkpointId}`;
    default:
      return `knowledge ${item.decisionKind} ${item.id}`;
  }
}

/** Client-side filter over the unified Resolved list. */
function filterDecisions(items: ResolvedDecision[], query: string): ResolvedDecision[] {
  const q = query.trim().toLowerCase();
  if (q.length === 0) return items;
  return items.filter((item) => resolvedHaystack(item).toLowerCase().includes(q));
}
