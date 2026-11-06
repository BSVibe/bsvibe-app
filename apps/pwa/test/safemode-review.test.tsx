import DeliveryRow from "@/components/decisions/DeliveryRow";
import { toPendingDecisions } from "@/lib/api/pending";
import type { PendingDelivery, ReviewFields, SafeModeItem } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

function delivery(review: ReviewFields): PendingDelivery {
  return {
    kind: "delivery",
    id: "delivery-1",
    itemId: "item-1",
    runId: "run-1",
    title: "Add export",
    detailHref: "/deliverables/d1",
    review,
    createdAt: "2026-10-30T00:00:00Z",
  };
}

describe("Safe Mode card — independent review", () => {
  it("shows the counts and the top findings as file:line — claim", () => {
    render(
      <DeliveryRow
        item={delivery({
          review_status: "done",
          review_counts: { high: 1, medium: 2, low: 0 },
          review_findings: [
            { severity: "high", file: "src/a.py", line: 12, claim: "totals double count" },
            { severity: "medium", file: "src/b.py", line: null, claim: "no timeout" },
          ],
        })}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText("Independent review: high 1 · medium 2 · low 0")).toBeInTheDocument();
    expect(screen.getByText("src/a.py:12 — totals double count")).toBeInTheDocument();
    expect(screen.getByText("src/b.py — no timeout")).toBeInTheDocument();
  });

  it("says the review could not be done when it is unavailable", () => {
    render(
      <DeliveryRow
        item={delivery({ review_status: "unavailable", review_counts: {}, review_findings: [] })}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText("The independent review could not be done")).toBeInTheDocument();
    expect(screen.queryByText(/Independent review:/)).toBeNull();
  });

  it("renders nothing when no review ran", () => {
    render(
      <DeliveryRow
        item={delivery({ review_status: "none", review_counts: {}, review_findings: [] })}
        onResolved={() => {}}
      />,
    );
    expect(screen.queryByText(/ndependent review/)).toBeNull();
  });

  it("carries the queue item's review onto the pending delivery", () => {
    const item = {
      id: "i1",
      deliverable_id: "d1",
      run_id: "r1",
      created_at: "2026-10-30T00:00:00Z",
      review_status: "done",
      review_counts: { high: 1, medium: 0, low: 0 },
      review_findings: [{ severity: "high", file: "a.py", line: 1, claim: "bad" }],
    } as unknown as SafeModeItem;
    const [pending] = toPendingDecisions([item], [], []);
    expect((pending as PendingDelivery).review?.review_status).toBe("done");
    expect((pending as PendingDelivery).review?.review_findings).toHaveLength(1);
  });
});
