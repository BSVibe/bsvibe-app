"use client";

import { DIRECT_SUBMITTED_EVENT } from "@/components/shell/DirectAction";
import { getBrief } from "@/lib/api/brief";
import type { BriefView } from "@/lib/api/types";
import { useSession } from "@/lib/auth/session";
import { useEventStream } from "@/lib/live-events/use-event-stream";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import BriefContent from "./BriefContent";

/** Container: loads the Brief view-model client-side, then renders it. It also
 *  re-reads on a successful Direct submission so a freshly-triggered run shows
 *  up in the lanes without a manual refresh. */
export default function Brief() {
  const [view, setView] = useState<BriefView | null>(null);
  const t = useTranslations("brief");

  const load = useCallback((onResult: (next: BriefView) => void) => {
    // A 401 propagates from getBrief() (auth expired): apiFetch has already
    // cleared the session and is redirecting to /login, so swallow the rejection
    // and stay on the calm loading splash while the page navigates away — rather
    // than surfacing an unhandled rejection or the demo board.
    getBrief()
      .then(onResult)
      .catch(() => {});
  }, []);

  useEffect(() => {
    let active = true;
    const apply = (next: BriefView) => {
      if (active) setView(next);
    };
    load(apply);
    // A Direct submission lands a new run server-side; re-read to reflect it.
    const onDirect = () => load(apply);
    window.addEventListener(DIRECT_SUBMITTED_EVENT, onDirect);
    return () => {
      active = false;
      window.removeEventListener(DIRECT_SUBMITTED_EVENT, onDirect);
    };
  }, [load]);

  // B16 — wake up on backend live events so the Brief lanes stay current
  // without a manual refresh. Each event just signals "refetch"; the Brief
  // GET endpoint remains the source of truth for the rendered view.
  const session = useSession();
  const refresh = useCallback(() => {
    load((next) => setView(next));
  }, [load]);
  useEventStream({
    token: session?.accessToken ?? null,
    onDecisionPending: refresh,
    onRunTerminal: refresh,
    onDeliveryQueued: refresh,
  });

  if (view === null) {
    return (
      <div className="brief brief--loading" aria-busy="true">
        <h1 className="brief__heading">{t("heading")}</h1>
        <p className="brief__loading-note">{t("loadingNote")}</p>
      </div>
    );
  }

  // A Safe-Mode approve/deny lands server-side; re-read so the resolved item
  // drops out of "Needs you" and any downstream lane reflects the dispatch.
  const onNeedsYouResolved = () => {
    load((next) => setView(next));
  };

  return <BriefContent view={view} onNeedsYouResolved={onNeedsYouResolved} />;
}
