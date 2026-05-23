"use client";

import { getActivity } from "@/lib/api/activity";
import type { ActivityRun } from "@/lib/api/types";
import { useEffect, useState } from "react";
import RunRow from "./RunRow";

/**
 * The Activity surface (the left-rail / mobile "Activity" route). A calm,
 * read-only history of everything the AI has done — every ExecutionRun, newest
 * first, each with its plain-language status and (on expand) its delivered
 * artifacts + proof.
 *
 * One up-front read (runs + products, composed in lib/api/activity.ts) drives
 * the list; each run's deliverables load lazily when its row is expanded so a
 * long history stays cheap. States:
 *
 *  - loading  → a quiet "Looking at recent activity…" note
 *  - error    → a calm inline note (never a blank page or an error wall)
 *  - empty    → "No runs yet — give me a Direction and they'll show up here."
 *  - ready    → the list of expandable run rows
 *
 * Read-only by design: there are no mutations on this surface.
 */
type Loaded = { state: "loading" } | { state: "error" } | { state: "ready"; runs: ActivityRun[] };

export default function Activity() {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });

  useEffect(() => {
    let active = true;
    getActivity()
      .then((runs) => {
        if (active) setLoaded({ state: "ready", runs });
      })
      .catch(() => {
        if (active) setLoaded({ state: "error" });
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="activity">
      <h1 className="activity__heading">Activity</h1>
      <p className="activity__lede">A look at what I&rsquo;ve been doing.</p>

      {loaded.state === "loading" && (
        <p className="activity__loading-note" aria-busy="true">
          Looking at recent activity…
        </p>
      )}

      {loaded.state === "error" && (
        <section className="activity-empty" aria-label="Activity">
          <p className="activity-empty__line">Couldn&rsquo;t load recent activity just now.</p>
          <p className="activity-empty__sub">Try again in a moment.</p>
        </section>
      )}

      {loaded.state === "ready" && loaded.runs.length === 0 && (
        <section className="activity-empty" aria-label="Activity">
          <p className="activity-empty__line">No runs yet.</p>
          <p className="activity-empty__sub">Give me a Direction and they&rsquo;ll show up here.</p>
        </section>
      )}

      {loaded.state === "ready" && loaded.runs.length > 0 && (
        <ul className="activity-list" aria-label="Recent runs">
          {loaded.runs.map((run) => (
            <RunRow key={run.runId} run={run} />
          ))}
        </ul>
      )}
    </div>
  );
}
