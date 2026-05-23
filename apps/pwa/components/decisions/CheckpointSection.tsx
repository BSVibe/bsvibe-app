"use client";

import { resolveCheckpoint } from "@/lib/api/checkpoints";
import type { Checkpoint } from "@/lib/api/types";
import { useState } from "react";

type RowState = "idle" | "resolving" | "resolved" | "error";

/**
 * "Decisions needed" — paused-run checkpoints. Each row shows the blocking
 * question (+ the agent's rationale, if any) and a text answer input. Resolve
 * records the answer and resumes the run; the resolved row drops out on the
 * container's re-read. In-flight, resolved, and calm inline-error states; a
 * failed resolve keeps the row actionable and never crashes the section.
 */
export default function CheckpointSection({
  items,
  onResolved,
}: {
  items: Checkpoint[];
  onResolved?: () => void;
}) {
  if (items.length === 0) return null;

  return (
    <section className="decisions-block" aria-label="Decisions needed">
      <header className="decisions-block__head">
        <h2 className="section-label">Decisions needed</h2>
        <span className="decisions-block__count">{items.length}</span>
      </header>
      <ul className="decisions-list">
        {items.map((item) => (
          <CheckpointRow key={item.id} item={item} onResolved={onResolved} />
        ))}
      </ul>
    </section>
  );
}

function CheckpointRow({ item, onResolved }: { item: Checkpoint; onResolved?: () => void }) {
  const [answer, setAnswer] = useState("");
  const [state, setState] = useState<RowState>("idle");

  const trimmed = answer.trim();

  async function resolve() {
    if (state === "resolving" || trimmed.length === 0) return;
    setState("resolving");
    try {
      await resolveCheckpoint(item.id, trimmed);
      setState("resolved");
      onResolved?.();
    } catch {
      setState("error");
    }
  }

  if (state === "resolved") {
    return (
      <li className="decisions-row decisions-row--resolved">
        <span className="decisions-row__q">{item.question || "Decision recorded."}</span>
        <span className="decisions-row__done" aria-live="polite">
          Answered — resuming the run.
        </span>
      </li>
    );
  }

  return (
    <li className="decisions-row">
      <p className="decisions-row__q">
        {item.question || "This run is paused and needs your input."}
      </p>
      {item.rationale ? <p className="decisions-row__why">{item.rationale}</p> : null}

      <div className="decisions-row__resolve">
        <textarea
          className="decisions-row__input"
          aria-label="Your answer"
          placeholder="Type your answer to resume the run…"
          rows={2}
          value={answer}
          disabled={state === "resolving"}
          onChange={(e) => setAnswer(e.target.value)}
        />
        <div className="decisions-row__foot">
          {state === "error" && (
            <span className="decisions-row__error" aria-live="polite">
              Couldn’t save that — please try again.
            </span>
          )}
          <button
            type="button"
            className="decisions-row__primary"
            onClick={resolve}
            disabled={state === "resolving" || trimmed.length === 0}
          >
            {state === "resolving" ? "Working…" : "Resolve"}
          </button>
        </div>
      </div>
    </li>
  );
}
