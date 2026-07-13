/**
 * Card-tap (stretched link) — tapping a card opens its report.
 *
 * The founder asked for the CARD itself to be the tap target (mobile-first) and
 * the redundant "View report" button GONE. Every card that has a report exposes
 * exactly ONE link — the title — whose ::after overlays the whole card, so any
 * non-button part of the card navigates to /deliverables/{id}. The action
 * buttons (approve / deny / …) sit ABOVE that overlay and must keep firing
 * their handlers instead of navigating — which means the buttons must never be
 * nested inside the link.
 */

import ShippedSection from "@/components/brief/ShippedSection";
import CheckpointRow from "@/components/decisions/CheckpointRow";
import DeliveryRow from "@/components/decisions/DeliveryRow";
import ResolvedRow from "@/components/decisions/ResolvedRow";
import ProductShipped from "@/components/products/ProductShipped";
import type {
  CheckpointAction,
  PendingCheckpoint,
  PendingDelivery,
  ResolvedDecision,
  ShippedItem,
  WorkStreamItem,
} from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const DELIVERY: PendingDelivery = {
  kind: "delivery",
  id: "delivery-1",
  itemId: "11111111-1111-1111-1111-111111111111",
  runId: "run-1",
  title: "Add the CSV export endpoint with pagination.",
  productSlug: "acme-corp",
  detailHref: "/deliverables/del-1",
  createdAt: "2026-05-24T08:00:00Z",
};

const ACTIONS: CheckpointAction[] = [
  { key: "ship", label_en: "Approve & ship", label_ko: "승인하고 출시" },
  { key: "discard", label_en: "Discard", label_ko: "폐기" },
];

const CHECKPOINT: PendingCheckpoint = {
  kind: "decision",
  id: "checkpoint-1",
  checkpointId: "22222222-2222-2222-2222-222222222222",
  title: "Add factorial(n) utility",
  question: "BSVibe couldn't verify this work — review it before it ships?",
  rationale: null,
  options: null,
  actions: ACTIONS,
  decision: "verification_failed",
  productSlug: "acme-corp",
  detailHref: "/deliverables/del-2",
  priorDecisions: [],
  createdAt: "2026-05-27T10:00:00Z",
};

const RESOLVED: ResolvedDecision = {
  kind: "delivery",
  id: "resolved-1",
  itemId: "33333333-3333-3333-3333-333333333333",
  title: "Ship the onboarding email copy.",
  status: "approved",
  productSlug: "acme-corp",
  detailHref: "/deliverables/del-3",
  resolvedAt: "2026-05-24T09:00:00Z",
};

const SHIPPED_ARTIFACT: ShippedItem = {
  id: "del-4",
  title: "Add the pricing page",
  productSlug: "acme-corp",
  source: "GitHub PR #15",
  artifactType: "pr",
  verdict: "This is verified",
  link: "https://github.com/acme/acme/pull/15",
};

const STREAM_SHIPPED: WorkStreamItem = {
  runId: "run-9",
  productSlug: "acme-corp",
  deliverableId: "del-5",
  status: "shipped",
  updatedAt: "2026-05-24T09:00:00Z",
  title: "Wire the signup flow",
  artifactType: "pr",
};

/** The single stretched link on a card — asserts there is exactly one <a>. */
function soleLink(card: HTMLElement): HTMLAnchorElement {
  const links = within(card).getAllByRole("link");
  expect(links).toHaveLength(1);
  return links[0] as HTMLAnchorElement;
}

describe("card tap → report (stretched link)", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("DeliveryRow (card WITH actions)", () => {
    it("exposes ONE link to the report, named by the card title", () => {
      vi.stubGlobal("fetch", vi.fn());
      const { container } = render(<DeliveryRow item={DELIVERY} onResolved={() => {}} />);
      const card = container.querySelector("li.need-card--delivery") as HTMLElement;
      expect(card).toBeTruthy();

      const link = soleLink(card);
      expect(link).toHaveAccessibleName("Add the CSV export endpoint with pagination.");
      expect(link).toHaveAttribute("href", "/deliverables/del-1");
    });

    it("drops the redundant 'View report' button", () => {
      vi.stubGlobal("fetch", vi.fn());
      render(<DeliveryRow item={DELIVERY} onResolved={() => {}} />);
      expect(screen.queryByText("View report")).not.toBeInTheDocument();
    });

    it("keeps Approve OUT of the link, so tapping it resolves instead of navigating", async () => {
      const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
      vi.stubGlobal("fetch", fetchMock);
      const onResolved = vi.fn();
      const { container } = render(<DeliveryRow item={DELIVERY} onResolved={onResolved} />);
      const card = container.querySelector("li.need-card--delivery") as HTMLElement;
      const link = soleLink(card);

      const approve = screen.getByRole("button", { name: "Approve" });
      const deny = screen.getByRole("button", { name: "Decline" });
      // Never nest a <button> inside the <a>: invalid HTML, and the link would
      // swallow the tap.
      expect(link).not.toContainElement(approve);
      expect(link).not.toContainElement(deny);

      await userEvent.click(approve);

      await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
      const url = String(fetchMock.mock.calls[0][0]);
      expect(url).toContain(`/safemode/${DELIVERY.itemId}/approve`);
    });
  });

  describe("CheckpointRow (card WITH actions)", () => {
    it("exposes ONE link to the report, named by the card title, and no 'View report'", () => {
      vi.stubGlobal("fetch", vi.fn());
      const { container } = render(<CheckpointRow item={CHECKPOINT} onResolved={() => {}} />);
      const card = container.querySelector("li.need-card--decision") as HTMLElement;

      const link = soleLink(card);
      expect(link).toHaveAccessibleName("Add factorial(n) utility");
      expect(link).toHaveAttribute("href", "/deliverables/del-2");
      expect(screen.queryByText("View report")).not.toBeInTheDocument();
    });

    it("keeps the one-click actions OUT of the link and still resolves on click", async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            id: CHECKPOINT.checkpointId,
            run_id: "run-1",
            status: "resolved",
            resolution: "ship",
            resolved_at: "2026-05-27T10:05:00Z",
            run_status: "shipped",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);
      const onResolved = vi.fn();
      const { container } = render(<CheckpointRow item={CHECKPOINT} onResolved={onResolved} />);
      const card = container.querySelector("li.need-card--decision") as HTMLElement;
      const link = soleLink(card);

      const ship = screen.getByRole("button", { name: "Approve & ship" });
      expect(link).not.toContainElement(ship);

      await userEvent.click(ship);

      await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
      const url = String(fetchMock.mock.calls[0][0]);
      expect(url).toContain(`/checkpoints/${CHECKPOINT.checkpointId}/resolve`);
    });
  });

  describe("ResolvedRow (card with NO actions)", () => {
    it("makes the row itself the link to the report", () => {
      const { container } = render(<ResolvedRow item={RESOLVED} />);
      const card = container.querySelector("li.decisions-row--resolved") as HTMLElement;

      const link = soleLink(card);
      expect(link).toHaveAccessibleName("Ship the onboarding email copy.");
      expect(link).toHaveAttribute("href", "/deliverables/del-3");
      expect(screen.queryByText("View report")).not.toBeInTheDocument();
      // The outcome stays as the subtitle.
      expect(screen.getByText("Delivery approved")).toBeInTheDocument();
    });
  });

  describe("ProductShipped (card with NO actions)", () => {
    it("links the row title to the report and drops the 'View report' link", () => {
      const { container } = render(<ProductShipped items={[SHIPPED_ARTIFACT]} />);
      const card = container.querySelector("li.product-shipped__row") as HTMLElement;

      const report = within(card).getByRole("link", { name: "Add the pricing page" });
      expect(report).toHaveAttribute("href", "/deliverables/del-4");
      expect(screen.queryByText("View report")).not.toBeInTheDocument();

      // The external artifact link survives — and is NOT nested in the report
      // link (it must stay tappable above the stretched overlay).
      const external = within(card).getByRole("link", { name: "Open artifact" });
      expect(external).toHaveAttribute("href", SHIPPED_ARTIFACT.link);
      expect(report).not.toContainElement(external);
    });
  });

  describe("ShippedSection (Brief rows)", () => {
    it("makes the whole row the tap target for its report", () => {
      const { container } = render(<ShippedSection items={[STREAM_SHIPPED]} />);
      const row = container.querySelector("li.stream__row") as HTMLElement;
      expect(row).toHaveClass("tap-card");

      const link = soleLink(row);
      expect(link).toHaveClass("tap-card__link");
      expect(link).toHaveAccessibleName("Wire the signup flow");
      expect(link).toHaveAttribute("href", "/deliverables/del-5");
    });
  });
});
