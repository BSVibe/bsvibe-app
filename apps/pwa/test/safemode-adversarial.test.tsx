import DeliveryRow from "@/components/decisions/DeliveryRow";
import AdversarialRounds from "@/components/runs/AdversarialRounds";
import { toPendingDecisions } from "@/lib/api/pending";
import type {
  PendingDelivery,
  ReviewFields,
  RunVerificationRound,
  SafeModeItem,
} from "@/lib/api/types";
import koMessages from "@/messages/ko.json";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
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

const F = (claim: string, over: object = {}) => ({
  severity: "high" as const,
  file: "src/a.py",
  line: 3,
  claim,
  ...over,
});

describe("Safe Mode card — adversarial check", () => {
  it("lists confirmed findings first and unreproduced ones apart as suspected", () => {
    const { container } = render(
      <DeliveryRow
        item={delivery({
          adversarial_status: "done",
          adversarial_confirmed: [F("cleanup never wired")],
          adversarial_suspected: [F("maybe racy", { severity: "low", line: null })],
        })}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText("Confirmed (1)")).toBeInTheDocument();
    expect(screen.getByText("Suspected (1)")).toBeInTheDocument();
    expect(screen.getByText("src/a.py:3 — cleanup never wired")).toBeInTheDocument();
    expect(screen.getByText("src/a.py — maybe racy")).toBeInTheDocument();
    const groups = [...container.querySelectorAll("[data-group]")].map((g) =>
      g.getAttribute("data-group"),
    );
    expect(groups).toEqual(["confirmed", "suspected"]);
  });

  it("says so when the check could not be done, and renders nothing when it did not run", () => {
    const { unmount } = render(
      <DeliveryRow item={delivery({ adversarial_status: "unavailable" })} onResolved={() => {}} />,
    );
    expect(screen.getByText("The adversarial check could not be done")).toBeInTheDocument();
    unmount();
    render(<DeliveryRow item={delivery({ adversarial_status: "none" })} onResolved={() => {}} />);
    expect(screen.queryByText(/adversarial/i)).toBeNull();
  });

  it("carries the queue item's adversarial fields onto the pending delivery", () => {
    const item = {
      id: "i1",
      deliverable_id: "d1",
      run_id: "r1",
      created_at: "2026-10-30T00:00:00Z",
      adversarial_status: "done",
      adversarial_confirmed: [F("bad")],
      adversarial_suspected: [],
    } as unknown as SafeModeItem;
    const [pending] = toPendingDecisions([item], [], []);
    const review = (pending as PendingDelivery).review;
    expect(review?.adversarial_status).toBe("done");
    expect(review?.adversarial_confirmed).toHaveLength(1);
  });
});

describe("Run detail — adversarial rounds", () => {
  const rounds: RunVerificationRound[] = [
    { round: 1, outcome: "failed" },
    {
      round: 2,
      outcome: "passed",
      adversarial: {
        status: "done",
        findings: [
          { ...F("only suspected", { severity: "high" }), confirmed: false },
          { ...F("proved defect", { severity: "low" }), confirmed: true },
        ],
      },
    },
    {
      round: 3,
      outcome: "passed",
      adversarial: { status: "unavailable", reason: "harness_error: tree changed" },
    },
  ];

  it("shows confirmed first and 의심 apart, in Korean, only for rounds that ran it", () => {
    const { container } = render(
      <NextIntlClientProvider locale="ko" messages={koMessages}>
        <AdversarialRounds rounds={rounds} />
      </NextIntlClientProvider>,
    );
    expect(container.querySelector('[data-round="1"]')).toBeNull();
    const round2 = container.querySelector('[data-round="2"]') as HTMLElement;
    expect(round2).toHaveTextContent("2번째 검증");
    const groups = [...round2.querySelectorAll("[data-group]")].map((g) =>
      g.getAttribute("data-group"),
    );
    expect(groups).toEqual(["confirmed", "suspected"]);
    expect(screen.getByText("확인됨 (1)")).toBeInTheDocument();
    expect(screen.getByText("의심 (1)")).toBeInTheDocument();
    expect(screen.getByText("src/a.py:3 — proved defect")).toBeInTheDocument();
    expect(screen.getByText(/깨 보는 검증을 하지 못했습니다/)).toBeInTheDocument();
    // the reason of an unavailable round is shown (harness_error / timeout / parse failure)
    expect(screen.getByTestId("adversarial-reason")).toHaveTextContent(
      "harness_error: tree changed",
    );
  });

  it("renders nothing without any adversarial round", () => {
    const { container } = render(
      <NextIntlClientProvider locale="ko" messages={koMessages}>
        <AdversarialRounds rounds={[{ round: 1, outcome: "passed" }]} />
      </NextIntlClientProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
