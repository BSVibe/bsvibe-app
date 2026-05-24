"use client";

import type { Proposal } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { proposalVerb } from "./Decisions";
import { relativeTime } from "./relative-time";

/**
 * One pending-proposal row in the inbox list. The whole row is a button that
 * opens the focused detail/resolve panel (Stitch Inbox → detail). Shows the
 * plain-language verb, a kind chip, and a relative timestamp + chevron — calm,
 * single-line, matching the designed inbox.
 */
export default function ProposalRow({ item, onOpen }: { item: Proposal; onOpen: () => void }) {
  const t = useTranslations("decisions");
  const verb = proposalVerb(item);

  return (
    <li className="decisions-row">
      <button
        type="button"
        className="decisions-row__open"
        // Verb is the row's accessible name so the list reads as the proposals.
        aria-label={verb}
        onClick={onOpen}
      >
        <span className="decisions-row__main">
          <span className="decisions-row__q">{verb}</span>
          <span className="decisions-row__meta">
            <span className="decisions-chip">{item.proposal_kind}</span>
            <span className="decisions-row__time">{relativeTime(item.created_at, t)}</span>
          </span>
        </span>
        <span className="decisions-row__chevron" aria-hidden="true">
          ›
        </span>
      </button>
    </li>
  );
}
