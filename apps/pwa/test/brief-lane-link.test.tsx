/**
 * Brief product lanes link into the per-product detail view. Asserts each lane
 * is a link to `/products/<slug>` while the lane content (name, status) is
 * unchanged — the minimal "wrap the lane in a link" change.
 */

import ProductLanes from "@/components/brief/ProductLanes";
import type { ProductLane } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const LANES: ProductLane[] = [
  { id: "l1", slug: "bsvibe-site", name: "bsvibe-site", state: "working", status: "writing tests" },
  { id: "l2", slug: "acme-corp", name: "acme-corp", state: "needs-you", status: "paused" },
  { id: "l3", slug: "stellar-app", name: "stellar-app", state: "idle", status: "—" },
];

describe("Brief product lanes → product detail link", () => {
  it("renders each lane as a link to its product detail page", () => {
    render(<ProductLanes lanes={LANES} />);

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(3);
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/products/bsvibe-site",
      "/products/acme-corp",
      "/products/stellar-app",
    ]);
  });

  it("keeps the lane content (name + plain-language status) inside the link", () => {
    render(<ProductLanes lanes={LANES} />);

    const link = screen.getByRole("link", { name: /bsvibe-site/ });
    expect(link).toHaveAttribute("href", "/products/bsvibe-site");
    expect(link).toHaveTextContent("bsvibe-site");
    expect(link).toHaveTextContent("writing tests");
  });

  it("still links an idle lane (whose status line is hidden)", () => {
    render(<ProductLanes lanes={LANES} />);

    const idle = screen.getByRole("link", { name: /stellar-app/ });
    expect(idle).toHaveAttribute("href", "/products/stellar-app");
  });
});
