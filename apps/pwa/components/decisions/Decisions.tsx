"use client";

import { listCheckpoints } from "@/lib/api/checkpoints";
import { ApiError } from "@/lib/api/client";
import { listPendingProposals } from "@/lib/api/decisions";
import type { Checkpoint, Proposal } from "@/lib/api/types";
import { setPendingDecisionsCount } from "@/lib/decisions/pending-count";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import CheckpointSection from "./CheckpointSection";
import ProposalSection from "./ProposalSection";

/**
 * The Decisions surface (the left-rail / mobile "Decisions" route). Two calm
 * sections, each consuming a REAL backend queue:
 *
 *  - "Decisions needed"  ← GET /api/v1/checkpoints  (paused-run checkpoints —
 *    the founder answers a blocking question to resume a stuck run)
 *  - "Knowledge review"  ← GET /api/v1/decisions?status_filter=pending  (canon
 *    merge proposals — Accept applies the merge, Reject leaves it untouched)
 *
 * Container loads both queues client-side. A single section failing (4xx /
 * network) degrades to an empty list for that section rather than blanking the
 * whole page — the other section still renders. Resolving an item re-reads so
 * the queue reflects server state.
 *
 * Safe-Mode delivery approvals stay in the Brief's "Needs you" (#29) — they are
 * deliberately NOT mirrored here.
 */
export default function Decisions() {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[] | null>(null);
  const [proposals, setProposals] = useState<Proposal[] | null>(null);
  const t = useTranslations("decisions");

  const load = useCallback((onResult: (c: Checkpoint[], p: Proposal[]) => void) => {
    Promise.all([
      listCheckpoints().catch(emptyOnApiError<Checkpoint>),
      listPendingProposals().catch(emptyOnApiError<Proposal>),
    ]).then(([c, p]) => {
      // Keep the nav badge in sync with the freshest read.
      setPendingDecisionsCount(c.length + p.length);
      onResult(c, p);
    });
  }, []);

  useEffect(() => {
    let active = true;
    load((c, p) => {
      if (!active) return;
      setCheckpoints(c);
      setProposals(p);
    });
    return () => {
      active = false;
    };
  }, [load]);

  const reload = useCallback(() => {
    load((c, p) => {
      setCheckpoints(c);
      setProposals(p);
    });
  }, [load]);

  if (checkpoints === null || proposals === null) {
    return (
      <div className="decisions decisions--loading" aria-busy="true">
        <h1 className="decisions__heading">{t("heading")}</h1>
        <p className="decisions__loading-note">{t("loadingNote")}</p>
      </div>
    );
  }

  const nothingPending = checkpoints.length === 0 && proposals.length === 0;

  return (
    <div className="decisions">
      <h1 className="decisions__heading">{t("heading")}</h1>

      {nothingPending ? (
        <section className="decisions-empty" aria-label={t("heading")}>
          <p className="decisions-empty__line">{t("emptyLine")}</p>
          <p className="decisions-empty__sub">{t("emptySub")}</p>
        </section>
      ) : (
        <>
          <CheckpointSection items={checkpoints} onResolved={reload} />
          <ProposalSection items={proposals} onResolved={reload} />
        </>
      )}
    </div>
  );
}

/** Swallow a per-surface ApiError / network blip into an empty list so one
 *  failing queue does not blank the whole Decisions page. */
function emptyOnApiError<T>(error: unknown): T[] {
  if (error instanceof ApiError || error instanceof TypeError) return [];
  throw error;
}
