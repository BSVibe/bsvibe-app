import type { BriefView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import NeedsYou from "./NeedsYou";
import ProductLanes from "./ProductLanes";
import RecentlyShipped from "./RecentlyShipped";

/**
 * The Glance, presentational (UX §3.4 layout): a centered column with the
 * "Needs you" strip on top, then product status lanes, then "Recently
 * shipped". Takes a ready `BriefView`, so it is trivially testable.
 *
 * `onNeedsYouResolved` bubbles up a successful Safe-Mode approve/deny so the
 * container can re-read the Brief and drop the resolved item.
 */
export default function BriefContent({
  view,
  onNeedsYouResolved,
}: {
  view: BriefView;
  onNeedsYouResolved?: () => void;
}) {
  const t = useTranslations("brief");
  return (
    <div className="brief">
      <h1 className="brief__heading">{t("heading")}</h1>
      <NeedsYou items={view.needsYou} onResolved={onNeedsYouResolved} />
      <ProductLanes lanes={view.lanes} />
      <RecentlyShipped items={view.recentlyShipped} />
    </div>
  );
}
