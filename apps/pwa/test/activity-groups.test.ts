import type { ProductDetailRun } from "@/lib/api/types";
/**
 * 활동 탭 그룹핑 — **많은 걸 전제로** 한 재설계의 순수 층 (#1042).
 *
 * 형님 실사용 피드백(2026-09-23)과 실측:
 *
 *   prod 런 291건: cancelled 210 (72%) · failed 56 · shipped 24
 *   누적 속도 6월 29 → 7월 54 → 8월 82 → 9월 45 = **월 ~50건**
 *   런 행에는 보존/정리 기제가 **없다** (정리되는 건 safe_mode_queue 뿐)
 *
 * ⇒ 100행 창은 두 달이면 다시 찬다. 지우든 필터하든 누적은 계속되므로
 *   형님 판단대로 **많은 걸 전제로** 그린다.
 *
 * 화면에서 보인 것: 16행 중 14행이 `중단됨`, `handoff 문서 첫 줄 보고` 가 **네 번**
 * 같은 제목으로 반복(9/17 5:50 둘, 6:06 둘). 시각 말고는 구분할 단서가 없었고,
 * 행동이 필요한 `검토가 필요해요` 한 줄이 그 사이에 같은 무게로 묻혔다.
 *
 * 순수 함수로 두는 이유 — 컴포넌트 안에 두면 렌더 테스트로만 닿고, 경계(날짜 넘김,
 * 연속 판정)는 렌더로 재기 어렵다.
 */
import { groupProductRuns } from "@/lib/runs/activity-groups";
import { describe, expect, it } from "vitest";

const NOW = new Date("2026-09-23T12:00:00Z");

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

describe("groupProductRuns", () => {
  it("행동이 필요한 런을 따로 위로 뽑는다", () => {
    const runs = [
      run({ updatedAt: "2026-09-23T10:00:00Z", status: "cancelled", title: "A" }),
      run({ updatedAt: "2026-09-22T10:00:00Z", status: "review_ready", title: "B" }),
      run({ updatedAt: "2026-09-21T10:00:00Z", status: "cancelled", title: "C" }),
    ];
    const { needsYou, groups } = groupProductRuns(runs, NOW);
    expect(needsYou.map((r) => r.run.title)).toEqual(["B"]);
    // 뽑힌 런은 날짜 그룹에 **다시 나타나지 않는다** (두 번 보이면 세는 게 틀린다)
    const inGroups = groups.flatMap((g) => g.rows.map((r) => r.run.title));
    expect(inGroups).not.toContain("B");
    expect(inGroups).toEqual(["A", "C"]);
  });

  it("⭐ 같은 제목·같은 상태가 연달아 오면 한 줄로 묶고 개수를 센다", () => {
    // 화면에서 네 번 반복되던 그 줄이다.
    const runs = [
      run({ updatedAt: "2026-09-17T09:06:00Z", title: "handoff 문서 첫 줄 보고" }),
      run({ updatedAt: "2026-09-17T09:06:00Z", title: "handoff 문서 첫 줄 보고" }),
      run({ updatedAt: "2026-09-17T08:50:00Z", title: "handoff 문서 첫 줄 보고" }),
      run({ updatedAt: "2026-09-17T08:50:00Z", title: "handoff 문서 첫 줄 보고" }),
    ];
    const { groups } = groupProductRuns(runs, NOW);
    const rows = groups.flatMap((g) => g.rows);
    expect(rows).toHaveLength(1);
    expect(rows[0].count).toBe(4);
    // 묶였어도 개별 런에 닿을 수 있어야 한다 — 디버깅 근거가 사라지면 안 된다
    expect(rows[0].runIds).toHaveLength(4);
  });

  it("제목이 다르면 안 묶는다", () => {
    const runs = [
      run({ updatedAt: "2026-09-17T09:00:00Z", title: "A" }),
      run({ updatedAt: "2026-09-17T08:00:00Z", title: "B" }),
    ];
    const rows = groupProductRuns(runs, NOW).groups.flatMap((g) => g.rows);
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.count === 1)).toBe(true);
  });

  it("⭐ 상태가 다르면 제목이 같아도 안 묶는다", () => {
    // 실패와 중단은 같은 제목이어도 **다른 사건**이다.
    const runs = [
      run({ updatedAt: "2026-09-17T09:00:00Z", title: "같은일", status: "cancelled" }),
      run({ updatedAt: "2026-09-17T08:00:00Z", title: "같은일", status: "failed" }),
    ];
    const rows = groupProductRuns(runs, NOW).groups.flatMap((g) => g.rows);
    expect(rows).toHaveLength(2);
  });

  it("⭐ 떨어져 있으면 안 묶는다 (연속일 때만)", () => {
    // 사이에 다른 일이 있었으면 그건 별개의 시도다. 다 묶으면 시간 흐름이 사라진다.
    const runs = [
      run({ updatedAt: "2026-09-17T09:00:00Z", title: "X" }),
      run({ updatedAt: "2026-09-17T08:30:00Z", title: "다른일" }),
      run({ updatedAt: "2026-09-17T08:00:00Z", title: "X" }),
    ];
    const rows = groupProductRuns(runs, NOW).groups.flatMap((g) => g.rows);
    expect(rows.map((r) => r.count)).toEqual([1, 1, 1]);
  });

  it("날짜로 묶고, 오늘/어제는 이름으로 부른다", () => {
    const runs = [
      run({ updatedAt: "2026-09-23T09:00:00Z", title: "오늘것" }),
      run({ updatedAt: "2026-09-22T09:00:00Z", title: "어제것" }),
      run({ updatedAt: "2026-09-10T09:00:00Z", title: "옛날것" }),
    ];
    const { groups } = groupProductRuns(runs, NOW);
    expect(groups).toHaveLength(3);
    expect(groups[0].kind).toBe("today");
    expect(groups[1].kind).toBe("yesterday");
    expect(groups[2].kind).toBe("date");
    // 날짜 그룹은 최신순
    expect(groups.flatMap((g) => g.rows.map((r) => r.run.title))).toEqual([
      "오늘것",
      "어제것",
      "옛날것",
    ]);
  });

  it("⭐ 입력이 최신순이 아니어도 그룹은 최신순이다", () => {
    // 2026-09-23 prod 실측에서 잡힌 결함. 배포된 화면의 날짜 소제목이 이랬다:
    //
    //   9월 4일 · 9월 18일 · 7월 23일 · 7월 22일 · 7월 31일 · 7월 7일 · 7월 6일
    //
    // 순수 층이 "입력은 이미 최신순이라 그 순서를 보존한다"를 **전제**로 깔았는데
    // prod 의 런 목록은 그렇지 않았다. 그리고 내 픽스처는 내가 최신순으로 만들어
    // 넣었기 때문에 그 전제가 거짓인 경우를 한 번도 안 봤다 —
    // **테스트가 프로덕션이 안 주는 것을 준** 그 함정이다(오늘 세 번째).
    //
    // ⇒ 전제를 없앤다. 입력 순서와 무관하게 그룹을 날짜 내림차순으로 낸다.
    const runs = [
      run({ updatedAt: "2026-09-04T10:00:00Z", title: "옛날" }),
      run({ updatedAt: "2026-09-18T10:00:00Z", title: "나중" }),
      run({ updatedAt: "2026-07-23T10:00:00Z", title: "더옛날" }),
    ];
    const { groups } = groupProductRuns(runs, NOW);
    expect(groups.map((g) => g.dateKey)).toEqual(["2026-09-18", "2026-09-04", "2026-07-23"]);
  });

  it("그룹 안의 행도 최신이 위다", () => {
    // 그룹 순서만 고치고 행 순서를 놔두면 같은 날 안에서 시간이 거꾸로 간다.
    // ⚠️ 두 시각은 **같은 로컬 날짜**여야 한다. 처음엔 08:00Z/20:00Z 를 썼는데
    //    KST(+9)로는 9/18 과 9/19 라 그룹이 갈렸고, 그걸 제품 버그로 오진할 뻔했다.
    //    날짜 경계를 다루는 테스트는 **자기 타임존에서 경계를 안 넘는지** 봐야 한다.
    const runs = [
      run({ updatedAt: "2026-09-18T01:00:00Z", title: "아침" }),
      run({ updatedAt: "2026-09-18T05:00:00Z", title: "저녁" }),
    ];
    const rows = groupProductRuns(runs, NOW).groups[0].rows;
    expect(rows.map((r) => r.run.title)).toEqual(["저녁", "아침"]);
  });

  it("빈 입력은 빈 결과 — 그리고 그건 오류가 아니다", () => {
    const { needsYou, groups } = groupProductRuns([], NOW);
    expect(needsYou).toEqual([]);
    expect(groups).toEqual([]);
  });

  it("⭐ 음성 대조군 — 전부 한 줄로 묶는 구현은 통과 못 한다", () => {
    const runs = [
      run({ updatedAt: "2026-09-17T09:00:00Z", title: "A" }),
      run({ updatedAt: "2026-09-17T08:00:00Z", title: "A" }),
      run({ updatedAt: "2026-09-16T09:00:00Z", title: "A" }),
    ];
    const { groups } = groupProductRuns(runs, NOW);
    // 날짜가 다르면 묶이지 않는다 — 날짜 경계가 연속을 끊는다
    expect(groups).toHaveLength(2);
    expect(groups[0].rows[0].count).toBe(2);
    expect(groups[1].rows[0].count).toBe(1);
  });
});
