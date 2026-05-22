"use client";

import { getBrief } from "@/lib/api/brief";
import type { BriefView } from "@/lib/api/types";
import { useEffect, useState } from "react";
import BriefContent from "./BriefContent";

/** Container: loads the Brief view-model client-side, then renders it. */
export default function Brief() {
  const [view, setView] = useState<BriefView | null>(null);

  useEffect(() => {
    let active = true;
    getBrief().then((next) => {
      if (active) setView(next);
    });
    return () => {
      active = false;
    };
  }, []);

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
