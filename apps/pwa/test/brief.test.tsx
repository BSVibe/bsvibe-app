import BriefContent from "@/components/brief/BriefContent";
import type { BriefView } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const VIEW: BriefView = {
  needsYou: [{ id: "n1", productSlug: "acme-corp", question: "which auth approach?" }],
  lanes: [
    {
      id: "l1",
      slug: "bsvibe-site",
      name: "bsvibe-site",
      state: "working",
      status: "writing tests · 4m in",
    },
    {
      id: "l2",
      slug: "acme-corp",
      name: "acme-corp",
      state: "needs-you",
      status: "paused — which auth approach?",
    },
    {
      id: "l3",
      slug: "stellar-app",
      name: "stellar-app",
      state: "shipped",
      status: "retry logic → PR #20 · verified",
    },
  ],
  recentlyShipped: [
    {
      id: "s1",
      title: "getRelatedPosts function",
      productSlug: "bsvibe-site",
      source: "GitHub PR #15",
      artifactType: "pr",
      verdict: "This is verified",
    },
  ],
  placeholder: true,
};

describe("Brief (Glance) surface", () => {
  it("shows the three sections: Needs you, Your products, Recently shipped", () => {
    render(<BriefContent view={VIEW} />);

    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Your products" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recently shipped" })).toBeInTheDocument();
  });

  it("renders the needs-you question and its count", () => {
    render(<BriefContent view={VIEW} />);

    expect(screen.getByText("which auth approach?")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("narrates each product lane in plain language with a state tag", () => {
    render(<BriefContent view={VIEW} />);

    expect(screen.getByText("bsvibe-site")).toBeInTheDocument();
    expect(screen.getByText("writing tests · 4m in")).toBeInTheDocument();
    expect(screen.getByText("needs you")).toBeInTheDocument();
    expect(screen.getByText("shipped")).toBeInTheDocument();
  });

  it("shows recently-shipped deliverables with a proof verdict", () => {
    render(<BriefContent view={VIEW} />);

    expect(screen.getByText("getRelatedPosts function")).toBeInTheDocument();
    expect(screen.getByText("bsvibe-site · GitHub PR #15")).toBeInTheDocument();
    expect(screen.getByText("This is verified")).toBeInTheDocument();
  });

  it("shows a calm empty state when nothing needs the founder", () => {
    render(<BriefContent view={{ ...VIEW, needsYou: [] }} />);

    expect(screen.getByText("Nothing needs you right now.")).toBeInTheDocument();
  });
});
