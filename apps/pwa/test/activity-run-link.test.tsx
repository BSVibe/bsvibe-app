/**
 * Activity → run-detail entry point. Each run row in the Activity list carries
 * an "Open run" link to /runs/{runId} — the inspectable run-detail surface
 * (Stitch "Triggered"). The expand/collapse deliverables affordance is
 * unchanged; the link is a separate, distinct target.
 */

import RunRow from "@/components/activity/RunRow";
import type { ActivityRun } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const RUN: ActivityRun = {
  runId: "run-1",
  productSlug: "quantum-link",
  status: "running",
  statusLabel: "Working",
  tone: "working",
  updatedAt: "2026-05-24T00:00:00Z",
};

describe("Activity run row → run-detail link", () => {
  it("links each run to /runs/{runId}", () => {
    render(
      <ul>
        <RunRow run={RUN} />
      </ul>,
    );

    const link = screen.getByRole("link", { name: /open run/i });
    expect(link).toHaveAttribute("href", "/runs/run-1");
  });
});
