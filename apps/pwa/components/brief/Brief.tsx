"use client";

import { DIRECT_SUBMITTED_EVENT } from "@/components/shell/DirectAction";
import { getBrief } from "@/lib/api/brief";
import type { BriefView } from "@/lib/api/types";
import { useCallback, useEffect, useState } from "react";
import BriefContent from "./BriefContent";

/** Container: loads the Brief view-model client-side, then renders it. It also
 *  re-reads on a successful Direct submission so a freshly-triggered run shows
 *  up in the lanes without a manual refresh. */
export default function Brief() {
  const [view, setView] = useState<BriefView | null>(null);

  const load = useCallback((onResult: (next: BriefView) => void) => {
    getBrief().then(onResult);
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

  if (view === null) {
    return (
      <div className="brief brief--loading" aria-busy="true">
        <h1 className="brief__heading">Brief</h1>
        <p className="brief__loading-note">Loading your products…</p>
      </div>
    );
  }

  return <BriefContent view={view} />;
}
