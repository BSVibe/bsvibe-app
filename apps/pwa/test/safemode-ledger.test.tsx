import DeliveryRow from "@/components/decisions/DeliveryRow";
import { toPendingDecisions } from "@/lib/api/pending";
import type { PendingDelivery, SafeModeItem } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

function delivery(ledger: PendingDelivery["ledger"]): PendingDelivery {
  return {
    kind: "delivery",
    id: "delivery-1",
    itemId: "item-1",
    runId: "run-1",
    title: "Add export",
    detailHref: "/deliverables/d1",
    ledger,
    createdAt: "2026-10-30T00:00:00Z",
  };
}

const counts = (done: number, unclear = 0, deferred = 0) => ({
  requirements_total: 8,
  requirements_done: done,
  requirements_unclear: unclear,
  requirements_deferred: deferred,
});

describe("Safe Mode card — requirements ledger", () => {
  it("shows unclear items before approval", () => {
    render(
      <DeliveryRow
        item={delivery({
          ledger_counts: counts(7, 1),
          ledger_attention: [{ id: "R1", text: "Export as CSV" }],
        })}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText("7 of 8 requirements confirmed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("R1: Export as CSV");
  });

  it("shows no warning when every requirement is confirmed", () => {
    render(
      <DeliveryRow
        item={delivery({ ledger_counts: counts(8), ledger_attention: [] })}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText("8 of 8 requirements confirmed")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders nothing for a run without a ledger", () => {
    render(
      <DeliveryRow
        item={delivery({ ledger_counts: {}, ledger_attention: [] })}
        onResolved={() => {}}
      />,
    );
    expect(screen.queryByText(/requirements confirmed/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("carries the queue item's ledger onto the pending delivery", () => {
    const item = {
      id: "i1",
      deliverable_id: "d1",
      run_id: "r1",
      created_at: "2026-10-30T00:00:00Z",
      ledger_counts: counts(7, 1),
      ledger_attention: [{ id: "R1", text: "Export as CSV" }],
    } as unknown as SafeModeItem;
    const [pending] = toPendingDecisions([item], [], []);
    expect((pending as PendingDelivery).ledger?.ledger_attention).toEqual([
      { id: "R1", text: "Export as CSV" },
    ]);
  });
});
