/**
 * GDPR L1 — /legal/sub-processors disclosure page.
 *
 * A calm, static, factual list of third-party sub-processors. No tracking,
 * no fancy UI — just an honest disclosure surface.
 */

import SubProcessorsPage from "@/app/legal/sub-processors/page";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

describe("Sub-processors disclosure page", () => {
  it("renders a heading naming sub-processors", () => {
    render(<SubProcessorsPage />);
    expect(screen.getByRole("heading", { name: /sub-processors/i })).toBeInTheDocument();
  });

  it("lists Supabase as a sub-processor", () => {
    render(<SubProcessorsPage />);
    // Multiple cells mention Supabase (name cell + purpose copy) — assert at
    // least one is present.
    expect(screen.getAllByText(/supabase/i).length).toBeGreaterThan(0);
  });

  it("each row exposes purpose + region columns", () => {
    render(<SubProcessorsPage />);
    // The page uses table semantics — assert the columns by their column
    // header role so the row-shape is locked in.
    expect(screen.getByRole("columnheader", { name: /purpose/i })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /region/i })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /sub-processor/i })).toBeInTheDocument();
  });
});
