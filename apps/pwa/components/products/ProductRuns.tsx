import type { ProductDetailRun } from "@/lib/api/types";
import { type ActivityRow, groupProductRuns } from "@/lib/runs/activity-groups";
import { STATUS_LABEL_KEY } from "@/lib/runs/status";
import { useTranslations } from "next-intl";
import Link from "next/link";

/** Calm absolute time ("2:14 PM") — the date now lives on the group heading,
 *  so the row only needs the clock. */
function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatDate(dateKey: string): string {
  const date = new Date(`${dateKey}T00:00:00`);
  if (Number.isNaN(date.getTime())) return dateKey;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * "활동" — 이 제품이 한 일. **많은 걸 전제로** 그린다 (#1042).
 *
 * 형님 실사용 피드백(2026-09-23)과 실측이 이 모양을 정했다:
 *
 *   prod 런 291건 중 `cancelled` 210 (72%) · 누적 월 ~50건 · 정리 기제 **없음**
 *
 * 그래서 100행 평평한 목록에서는 16행 중 14행이 `중단됨`이었고, 같은 제목이
 * 네 번 반복됐으며, 행동이 필요한 한 줄이 그 사이에 같은 무게로 묻혔다.
 *
 * 세 가지로 푼다 — 로직은 `lib/runs/activity-groups`(순수, 테스트됨)에 있다:
 *   1. 행동이 필요한 런을 **위로**
 *   2. 나머지를 **날짜로** 묶고
 *   3. **연속된** 같은 제목·상태를 접어 개수를 보인다
 *
 * 접힌 줄도 개별 런 id 를 들고 있다 — 과거 런 기록은 판정 근거로 실제로 쓰인다.
 */
export default function ProductRuns({ runs }: { runs: ProductDetailRun[] }) {
  const t = useTranslations("products");
  // 상태 라벨은 Brief/Activity 와 **같은 어휘**를 쓴다(공유 키).
  const tBrief = useTranslations("brief");
  const { needsYou, groups } = groupProductRuns(runs);

  const renderRow = (row: ActivityRow) => {
    const { run, count } = row;
    const body = (
      <>
        <span className={`product-run__status product-run__status--${run.tone}`}>
          {tBrief(STATUS_LABEL_KEY[run.status])}
        </span>
        {run.title && <span className="product-run__title">{run.title}</span>}
        {count > 1 && <span className="product-run__repeat">{t("repeatedTimes", { count })}</span>}
        <span className="product-run__when">{formatTime(run.updatedAt)}</span>
      </>
    );
    return (
      <li key={run.runId} className="product-run">
        {run.detailHref ? (
          <Link className="product-run__link" href={run.detailHref}>
            {body}
          </Link>
        ) : (
          body
        )}
      </li>
    );
  };

  return (
    <section className="product-runs" aria-label={t("recentRuns")}>
      <h2 className="section-label">{t("recentRuns")}</h2>
      {runs.length === 0 ? (
        <p className="product-runs__empty">{t("noRuns")}</p>
      ) : (
        <>
          {needsYou.length > 0 && (
            <div className="product-runs__needs-you">
              <h3 className="product-runs__group-label">{t("needsYou")}</h3>
              <ul className="product-runs__list">{needsYou.map(renderRow)}</ul>
            </div>
          )}
          {groups.map((group) => (
            <div key={group.dateKey} className="product-runs__group">
              <h3 className="product-runs__group-label">
                {group.kind === "today"
                  ? t("today")
                  : group.kind === "yesterday"
                    ? t("yesterday")
                    : formatDate(group.dateKey)}
              </h3>
              <ul className="product-runs__list">{group.rows.map(renderRow)}</ul>
            </div>
          ))}
        </>
      )}
    </section>
  );
}
