import type { ProductDetailRun } from "@/lib/api/types";

/**
 * 활동 탭의 그룹핑 — **많은 걸 전제로** 한 재설계의 순수 층 (#1042).
 *
 * 형님 실사용 피드백(2026-09-23)과 실측이 이 모양을 정했다:
 *
 *   prod 런 291건 중 `cancelled` 210 (72%). 누적 속도 월 ~50건.
 *   런 행에는 **보존/정리 기제가 없다** — 정리되는 건 safe_mode_queue 뿐이다.
 *
 * ⇒ 100행 창은 두 달이면 다시 찬다. 지우든 필터하든 누적은 계속되므로,
 *   "적게 만드는" 대신 **많아도 읽히게** 그린다.
 *
 * 화면에서 보인 것: 16행 중 14행이 `중단됨`, 같은 제목이 네 번 반복. 시각 말고는
 * 구분할 단서가 없었고, 행동이 필요한 한 줄이 그 사이에 같은 무게로 묻혔다.
 *
 * 세 가지를 한다:
 *   1. 행동이 필요한 런(`review_ready`)을 **위로 뽑는다** — 날짜 그룹엔 다시 안 낸다
 *   2. 나머지를 **날짜로** 묶는다 (오늘 / 어제 / 그 외)
 *   3. **연속된** 같은 제목·같은 상태를 한 줄로 접고 개수를 센다
 *
 * ⚠️ 접는 것은 **연속일 때만**이다. 떨어진 것을 다 묶으면 시간 흐름이 사라지고,
 *    "두 번 시도했다"와 "이틀에 걸쳐 네 번 시도했다"가 같은 줄이 된다.
 *    그리고 접힌 줄도 `runIds` 로 개별 런에 닿을 수 있다 — 디버깅 근거는 남겨야 한다.
 *    (오늘만 해도 과거 런 기록으로 두 번 판정했다.)
 */

export type ActivityRow = {
  /** 접힌 묶음의 대표(가장 최근). */
  run: ProductDetailRun;
  /** 접힌 런 개수. 1 이면 안 접혔다는 뜻. */
  count: number;
  /** 접힌 런 전부의 id — 개별 런으로 가는 길을 잃지 않는다. */
  runIds: string[];
};

export type ActivityGroup = {
  kind: "today" | "yesterday" | "date";
  /** 그 그룹의 날짜(로컬 자정 기준 ISO date, `YYYY-MM-DD`). */
  dateKey: string;
  rows: ActivityRow[];
};

function dateKeyOf(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  // 로컬 날짜로 센다 — 형님이 보는 것은 형님의 자정 경계다.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function kindOf(dateKey: string, now: Date): ActivityGroup["kind"] {
  const today = dateKeyOf(now.toISOString());
  if (dateKey === today) return "today";
  const y = new Date(now);
  y.setDate(y.getDate() - 1);
  if (dateKey === dateKeyOf(y.toISOString())) return "yesterday";
  return "date";
}

/** 연속된 같은 (제목, 상태) 를 한 줄로 접는다. 떨어진 것은 안 접는다. */
function collapseConsecutive(runs: ProductDetailRun[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  for (const run of runs) {
    const last = rows[rows.length - 1];
    if (last && last.run.title === run.title && last.run.status === run.status) {
      last.count += 1;
      last.runIds.push(run.runId);
      continue;
    }
    rows.push({ run, count: 1, runIds: [run.runId] });
  }
  return rows;
}

/** 행동이 필요한 상태 — 이것만 위로 뽑는다. */
function needsAction(run: ProductDetailRun): boolean {
  return run.status === "review_ready";
}

export function groupProductRuns(
  runs: ProductDetailRun[],
  now: Date = new Date(),
): { needsYou: ActivityRow[]; groups: ActivityGroup[] } {
  const needsYouRuns = runs.filter(needsAction);
  const rest = runs.filter((r) => !needsAction(r));

  // 날짜별로 모은다. 입력은 이미 최신순이라 그 순서를 보존한다.
  const byDate = new Map<string, ProductDetailRun[]>();
  for (const run of rest) {
    const key = dateKeyOf(run.updatedAt);
    const bucket = byDate.get(key);
    if (bucket) bucket.push(run);
    else byDate.set(key, [run]);
  }

  const groups: ActivityGroup[] = [];
  for (const [dateKey, items] of byDate) {
    groups.push({ kind: kindOf(dateKey, now), dateKey, rows: collapseConsecutive(items) });
  }

  return {
    // 행동이 필요한 줄은 접지 않는다 — 각각이 형님의 결정을 기다린다.
    needsYou: needsYouRuns.map((run) => ({ run, count: 1, runIds: [run.runId] })),
    groups,
  };
}
