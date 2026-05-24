/**
 * Brief → Delivery Report entry point. Each recently-shipped deliverable row
 * carries a "View report" link to /deliverables/{id} — the glass-box proof.
 */

import RecentlyShipped from "@/components/brief/RecentlyShipped";
import type { ShippedItem } from "@/lib/api/types";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const ITEMS: ShippedItem[] = [
  {
    id: "d1",
    title: "getRelatedPosts function",
    productSlug: "bsvibe-site",
    source: "opened a pull request",
    artifactType: "pr",
    verdict: "This is verified",
    link: "https://github.com/acme/repo/pull/15",
  },
];

describe("Brief recently-shipped → report link", () => {
  it("renders a View report link to /deliverables/{id} for each shipped item", () => {
    render(<RecentlyShipped items={ITEMS} />);

    const list = screen.getByRole("region", { name: /recently shipped/i });
    const link = within(list).getByRole("link", { name: /view report|see the proof/i });
    expect(link).toHaveAttribute("href", "/deliverables/d1");
  });

  it("renders nothing when there are no shipped items", () => {
    const { container } = render(<RecentlyShipped items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
