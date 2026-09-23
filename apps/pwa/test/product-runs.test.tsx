/**
 * ProductRuns — 순수 층이 정한 구조가 **화면에 실제로 나타나는지** (#1042).
 *
 * `activity-groups.test.ts` 는 로직을 잰다. 로직이 맞아도 컴포넌트가 그걸 안 쓰면
 * 화면은 그대로다 — 이 레포는 "공유 함수의 가드는 호출자 배선을 증명 못 한다"를
 * 스킬로 갖고 있다. 그래서 렌더로 한 번 더 본다.
 */
import ProductRuns from "@/components/products/ProductRuns";
import type { ProductDetailRun } from "@/lib/api/types";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

function run(over: Partial<ProductDetailRun> & { updatedAt: string }): ProductDetailRun {
  return {
    runId: over.runId ?? Math.random().toString(36).slice(2),
    status: "cancelled",
    tone: "neutral",
    shipped: false,
    title: "작업",
    detailHref: null,
    ...over,
  } as ProductDetailRun;
}

describe("ProductRuns", () => {
  it("행동이 필요한 런을 별도 구역에 위로 올린다", () => {
    const now = new Date();
    const iso = (d: Date) => d.toISOString();
    render(
      <ProductRuns
        runs={[
          run({ updatedAt: iso(now), title: "그냥작업" }),
          run({ updatedAt: iso(now), title: "검토대상", status: "review_ready", tone: "review" }),
        ]}
      />,
    );
    const section = screen.getByRole("region", { name: /Recent runs/i });
    const headings = within(section)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings[0]).toMatch(/Needs you/i);
    // 그리고 그 구역 안에 그 런이 있다
    expect(within(section).getByText("검토대상")).toBeInTheDocument();
  });

  it("⭐ 반복된 같은 작업을 한 줄로 접고 개수를 보인다", () => {
    const now = new Date();
    const iso = (d: Date) => d.toISOString();
    render(
      <ProductRuns
        runs={[
          run({ updatedAt: iso(now), title: "handoff 문서 첫 줄 보고" }),
          run({ updatedAt: iso(now), title: "handoff 문서 첫 줄 보고" }),
          run({ updatedAt: iso(now), title: "handoff 문서 첫 줄 보고" }),
        ]}
      />,
    );
    const section = screen.getByRole("region", { name: /Recent runs/i });
    // 한 줄만 있다 (세 줄이 아니라)
    expect(within(section).getAllByText("handoff 문서 첫 줄 보고")).toHaveLength(1);
    expect(within(section).getByText(/×3|3번/)).toBeInTheDocument();
  });

  it("음성 대조군 — 안 반복된 줄에는 개수 배지가 없다", () => {
    const now = new Date();
    render(<ProductRuns runs={[run({ updatedAt: now.toISOString(), title: "한번만" })]} />);
    const section = screen.getByRole("region", { name: /Recent runs/i });
    expect(within(section).queryByText(/×\d|\d번 반복/)).toBeNull();
  });

  it("날짜로 묶고 오늘은 오늘이라 부른다", () => {
    const now = new Date();
    const older = new Date(now);
    older.setDate(older.getDate() - 5);
    render(
      <ProductRuns
        runs={[
          run({ updatedAt: now.toISOString(), title: "오늘것" }),
          run({ updatedAt: older.toISOString(), title: "옛날것" }),
        ]}
      />,
    );
    const section = screen.getByRole("region", { name: /Recent runs/i });
    const headings = within(section)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings[0]).toMatch(/Today/i);
    expect(headings).toHaveLength(2);
  });

  it("빈 상태는 조용하다", () => {
    render(<ProductRuns runs={[]} />);
    expect(screen.getByText(/No runs for this product yet/i)).toBeInTheDocument();
  });
});
