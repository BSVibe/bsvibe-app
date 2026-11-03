/** 런 행의 검증 회차 문구 + 제품 상단 1라운드 통과율. */
import ProductHeader from "@/components/products/ProductHeader";
import ProductRuns from "@/components/products/ProductRuns";
import type { ProductDetailRun, ProductDetailView } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

function run(title: string, over: Partial<ProductDetailRun>): ProductDetailRun {
  return {
    runId: title,
    status: "shipped",
    tone: "shipped",
    shipped: true,
    title,
    detailHref: null,
    updatedAt: new Date().toISOString(),
    ...over,
  };
}

describe("ProductRuns 검증 회차", () => {
  it("행마다 맞는 문구를 보이고, 검증이 없으면 아무것도 보이지 않는다", () => {
    render(
      <ProductRuns
        runs={[
          run("first", { verificationRounds: 1, passedOnRound: 1 }),
          run("third", { verificationRounds: 3, passedOnRound: 3 }),
          run("never", {
            verificationRounds: 2,
            passedOnRound: null,
            status: "failed",
            tone: "failed",
            shipped: false,
          }),
          run("none", { verificationRounds: 0, passedOnRound: null }),
        ]}
      />,
    );
    expect(screen.getAllByText("Passed on the 1st try")).toHaveLength(1);
    expect(screen.getByText("Passed on try 3")).toBeInTheDocument();
    expect(screen.getByText("Verified 2×")).toBeInTheDocument();
    expect(document.querySelectorAll(".product-run__rounds")).toHaveLength(3);
  });
});

describe("ProductHeader 1라운드 통과율", () => {
  const view = (rate: ProductDetailView["firstRoundPassRate"]): ProductDetailView => ({
    id: "p",
    slug: "p",
    name: "P",
    repoUrl: null,
    currentStatusKey: "headlineEmpty",
    currentTone: "neutral",
    runs: [],
    shipped: [],
    firstRoundPassRate: rate,
  });

  it("통과율 한 줄을 보인다", () => {
    render(
      <ProductHeader
        view={view({
          runs_considered: 6,
          runs_verified: 4,
          passed_first_round: 3,
          pass_rate: 0.75,
        })}
      />,
    );
    expect(screen.getByText("3 of the last 4 runs passed on round 1")).toBeInTheDocument();
  });

  it("검증된 런이 없으면 아무것도 보이지 않는다", () => {
    render(
      <ProductHeader
        view={view({
          runs_considered: 2,
          runs_verified: 0,
          passed_first_round: 0,
          pass_rate: null,
        })}
      />,
    );
    expect(document.querySelector(".product-head__pass-rate")).toBeNull();
  });
});
