"use client";

import { acceptProposal, rejectProposal } from "@/lib/api/decisions";
import type { Proposal } from "@/lib/api/types";
import { useState } from "react";

type RowState = "idle" | "accepting" | "rejecting" | "accepted" | "rejected" | "error";

/** Plain-language summary of a canon proposal — what the merge would do.
 *  `action_kind` (e.g. `merge-concepts`) is the verb; `id` is the proposal's
 *  vault path (`proposals/<kind>/<file>.md`). */
function describe(p: Proposal): string {
  const verb = p.action_kind.replace(/-/g, " ");
  return `${verb} → ${p.id}`;
}

/**
 * "Knowledge review" — pending canonicalization proposals. Each row shows the
 * proposed merge and Accept / Reject. Accept applies the linked typed actions
 * (collapses a variant onto its canonical anchor); Reject leaves the graph
 * untouched. In-flight, resolved, and calm inline-error states; the resolved
 * row drops out on the container's re-read.
 */
export default function ProposalSection({
  items,
  onResolved,
}: {
  items: Proposal[];
  onResolved?: () => void;
}) {
  if (items.length === 0) return null;

  return (
    <section className="decisions-block" aria-label="Knowledge review">
      <header className="decisions-block__head">
        <h2 className="section-label">Knowledge review</h2>
        <span className="decisions-block__count">{items.length}</span>
      </header>
      <ul className="decisions-list">
        {items.map((item) => (
          <ProposalRow key={item.id} item={item} onResolved={onResolved} />
        ))}
      </ul>
    </section>
  );
}

function ProposalRow({ item, onResolved }: { item: Proposal; onResolved?: () => void }) {
  const [state, setState] = useState<RowState>("idle");
  const busy = state === "accepting" || state === "rejecting";

  // The accept/reject endpoints address a proposal by its vault path, which the
  // list surfaces as `id` (e.g. `proposals/merge-concepts/<file>.md`).
  // (`action_path` is the LINKED ACTION draft `actions/<kind>/...` — a different
  // handle that would 404 against the `proposals/`-only resolve guard.)
  const handle = item.id;

  async function run(action: "accept" | "reject") {
    if (busy) return;
    setState(action === "accept" ? "accepting" : "rejecting");
    try {
      if (action === "accept") {
        await acceptProposal(handle);
        setState("accepted");
      } else {
        await rejectProposal(handle);
        setState("rejected");
      }
      onResolved?.();
    } catch {
      setState("error");
    }
  }

  if (state === "accepted" || state === "rejected") {
    return (
      <li className={`decisions-row decisions-row--${state}`}>
        <span className="decisions-row__q">{describe(item)}</span>
        <span className="decisions-row__done" aria-live="polite">
          {state === "accepted" ? "Merged into your knowledge." : "Left as-is."}
        </span>
      </li>
    );
  }

  return (
    <li className="decisions-row">
      <p className="decisions-row__q">{describe(item)}</p>
      <p className="decisions-row__why">
        Merging keeps your knowledge graph from holding the same idea twice.
      </p>

      <div className="decisions-row__foot">
        {state === "error" && (
          <span className="decisions-row__error" aria-live="polite">
            Couldn’t do that — please try again.
          </span>
        )}
        <button
          type="button"
          className="decisions-row__secondary"
          onClick={() => run("reject")}
          disabled={busy}
        >
          {state === "rejecting" ? "Working…" : "Reject"}
        </button>
        <button
          type="button"
          className="decisions-row__primary"
          onClick={() => run("accept")}
          disabled={busy}
        >
          {state === "accepting" ? "Working…" : "Accept"}
        </button>
      </div>
    </li>
  );
}
